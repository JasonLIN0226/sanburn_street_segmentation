"""Project launcher for pixel editing in LabelMe 7.11.0."""

import math
from dataclasses import replace

import numpy as np
from labelme import __main__ as cli
from labelme import __version__
from labelme._shape import Shape
from PIL import Image, ImageDraw
from PySide6 import QtCore, QtGui, QtWidgets

Qt = QtCore.Qt


def apply_stroke(old, size, points, radius, paint):
    """Return the updated st1 shape, None if erased, or old for a no-op."""
    image = Image.new("L", size)
    if old is not None:
        origin = tuple(np.rint(old.points[0]).astype(int))
        image.paste(Image.fromarray(old.mask.astype(np.uint8)), origin)
    x, y = zip(*points)
    box = (
        max(0, min(x) - radius),
        max(0, min(y) - radius),
        min(size[0], max(x) + radius + 1),
        min(size[1], max(y) + radius + 1),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        return old
    before = image.crop(box)
    draw = ImageDraw.Draw(image)
    value = int(paint)
    draw.line(points, fill=value, width=2 * radius + 1, joint="curve")
    for px, py in points:
        draw.ellipse((px - radius, py - radius, px + radius, py + radius), fill=value)
    if np.array_equal(np.asarray(before), np.asarray(image.crop(box))):
        return old
    bounds = image.getbbox()
    if bounds is None:
        return None
    x0, y0, x1, y1 = bounds
    base = old if old is not None else Shape(label="st1", shape_type="mask", closed=True)
    return replace(
        base, mask=np.asarray(image.crop(bounds), dtype=bool), points=[[x0, y0], [x1 - 1, y1 - 1]]
    )


class StrokeOverlay(QtWidgets.QWidget):
    """Mouse-transparent preview in the same zoomed coordinates as LabelMe."""

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.points = []
        self.hover = None
        self.radius = 12
        self.paint = True
        self.setGeometry(canvas.rect())

    def paintEvent(self, event):
        if self.hover is None:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        canvas = self.canvas
        painter.translate(canvas.transform_image_point_to_widget(point=QtCore.QPointF()))
        painter.scale(canvas.scale, canvas.scale)
        painter.translate(0.5, 0.5)
        color = QtGui.QColor(30, 212, 115, 160) if self.paint else QtGui.QColor(246, 83, 65, 170)
        radius = self.radius + 0.5
        if self.points:
            pen = QtGui.QPen(
                color,
                2 * radius,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
            painter.setPen(pen)
            path = QtGui.QPainterPath(QtCore.QPointF(*self.points[0]))
            for point in self.points[1:]:
                path.lineTo(QtCore.QPointF(*point))
            if len(self.points) == 1:
                painter.drawPoint(QtCore.QPointF(*self.points[0]))
            else:
                painter.drawPath(path)
        painter.setPen(QtGui.QPen(QtGui.QColor("white"), 1.5 / canvas.scale))
        painter.drawEllipse(QtCore.QPointF(*self.hover), radius, radius)
        painter.end()


class BrushWindow(cli.MainWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.canvas = self._canvas_widgets.canvas
        self.preview = StrokeOverlay(self.canvas)
        self.stroke_target = None
        self.bar = self.addToolBar("Mask 修补")
        self.bar.setObjectName("streetMaskBrush")
        self.tools = QtGui.QActionGroup(self)
        self.tools.setExclusionPolicy(QtGui.QActionGroup.ExclusionPolicy.ExclusiveOptional)
        for title in ("画笔", "橡皮"):
            action = self.bar.addAction(title)
            action.setCheckable(True)
            self.tools.addAction(action)
        self.tools.triggered.connect(self.set_brush)
        self.bar.addWidget(QtWidgets.QLabel("  半径 "))
        radius = QtWidgets.QSpinBox()
        radius.setRange(1, 300)
        radius.setSuffix(" px")
        radius.setValue(self.preview.radius)
        radius.valueChanged.connect(self.resize_brush)
        self.bar.addWidget(radius)
        self.canvas.installEventFilter(self)
        for _, action in self._actions.draw:
            action.triggered.connect(self.stop_brush)
        self._actions.edit_mode.triggered.connect(self.stop_brush)
        self.tools.actions()[0].trigger()

    def resize_brush(self, radius):
        self.cancel_stroke()
        self.preview.radius = radius

    def cancel_stroke(self):
        self.stroke_target = None
        self.preview.points.clear()
        self.preview.hover = None
        self.preview.update()

    def stop_brush(self):
        self.cancel_stroke()
        action = self.tools.checkedAction()
        if action:
            action.setChecked(False)
        self.preview.hide()

    def set_brush(self, action):
        self.cancel_stroke()
        self.preview.setVisible(action.isChecked())
        if action.isChecked():
            self._switch_canvas_mode(edit=True, create_mode=None)
            self.preview.paint = action == self.tools.actions()[0]

    def eventFilter(self, watched, event):
        if watched is not getattr(self, "canvas", None) or not hasattr(self, "tools"):
            return super().eventFilter(watched, event)
        kind = event.type()
        E = QtCore.QEvent.Type
        if kind == E.Resize:
            self.preview.setGeometry(self.canvas.rect())
        if not self.tools.checkedAction() or self.canvas.pixmap.isNull():
            return super().eventFilter(watched, event)
        if kind == E.Leave and not self.stroke_target:
            self.preview.hover = None
            self.preview.update()
        if kind == E.KeyPress and event.key() == Qt.Key.Key_Escape:
            self.stop_brush()
            return True
        if kind not in (
            E.MouseMove,
            E.MouseButtonPress,
            E.MouseButtonRelease,
            E.MouseButtonDblClick,
        ):
            return super().eventFilter(watched, event)
        if kind != E.MouseMove and event.button() != Qt.MouseButton.LeftButton:
            return False
        if kind == E.MouseMove and event.buttons() & ~Qt.MouseButton.LeftButton:
            return False
        point = self.canvas.transform_widget_point_to_image(point=event.position())
        pixel = math.floor(point.x()), math.floor(point.y())
        preview = self.preview
        preview.hover = pixel
        if kind in (E.MouseButtonPress, E.MouseButtonDblClick):
            if self.canvas.is_out_of_pixmap(p=point) or self.canvas.is_drawing:
                return True
            matches = [s for s in self.canvas.shapes if s.label == "st1"]
            old = matches[0] if matches else None
            if len(matches) > 1 or (
                old and (old.shape_type != "mask" or old.locked or not old.visible)
            ):
                self.statusBar().showMessage("请选择含单个可见、未锁定 st1 mask 的标注。", 5000)
                return True
            self.stroke_target = (self.canvas.pixmap.cacheKey(), old)
            preview.points = [pixel]
        elif self.stroke_target:
            if preview.points[-1] != pixel:
                preview.points.append(pixel)
            if kind == E.MouseButtonRelease:
                key, old = self.stroke_target
                shapes = self.canvas.shapes
                current = [s for s in shapes if s.label == "st1"]
                if key == self.canvas.pixmap.cacheKey() and current == ([old] if old else []):
                    size = self.canvas.pixmap.size()
                    new = apply_stroke(
                        old,
                        (size.width(), size.height()),
                        preview.points,
                        preview.radius,
                        preview.paint,
                    )
                    if new is not old:
                        revised = [new if s is old else s for s in shapes]
                        if old is None:
                            revised.append(new)
                        self._commit_shapes(shapes=[s for s in revised if s is not None])
                self.cancel_stroke()
        preview.update()
        return True


if __name__ == "__main__":
    if __version__ != "7.11.0":
        raise SystemExit(f"This mask brush launcher requires LabelMe 7.11.0 (found {__version__})")
    cli.MainWindow = BrushWindow
    cli.main()
