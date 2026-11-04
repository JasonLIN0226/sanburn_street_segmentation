"""CLI for an agent-provided street inventory; never invent visual receipts."""
import argparse
import json
from pathlib import Path

import boundary_pairs as bp
import regions
import workflow as wf


def prepare(source, output, name, tile=2048, overlap=128):
    wf.safe_id(name)
    root = Path(output).resolve()
    if root.exists():
        raise FileExistsError('choose a new experiment directory')
    root.mkdir(parents=True)
    (root/'maps').mkdir()
    (root/'reviews').mkdir()
    work = root/'maps'/name
    state = wf.init(work, source, tile=tile, overlap=overlap)
    state['strict_visual_gate'] = True
    wf.save(work/'workflow.json', state)
    for t in state['tiles']:
        wf.make_crop(work, state, t)
    return {'source_tiles':str(work/'source_tiles'), 'workflow':str(work/'workflow.json'),
            'status':'awaiting_agent_source_observations'}


def propose(root, name, inventory, pairs):
    wf.safe_id(name)
    root = Path(root).resolve()
    work = root/'maps'/name
    state = wf.verified(work)
    if state['phase'] != 'source':
        raise ValueError('proposal requires a freshly prepared source phase')
    observed = wf.load(inventory)
    pair_spec = wf.load(pairs)
    # Every receipt must come from an agent that viewed the matching source tile.
    if set(observed.get('tiles', {})) != {t['id'] for t in state['tiles']}:
        raise ValueError('agent observations must cover every configured source tile')
    observed = bp.apply_pairs(observed, pair_spec, state['size_wh'])
    wf.save(root/'reviews'/f'{name}_source.json', observed)
    wf.save(root/'reviews'/f'{name}_boundary_pairs.json', pair_spec)
    wf.save(root/'reviews'/f'{name}_boundary_evidence.json', bp.boundary_evidence(state['source'], pair_spec))
    regions.ROOT = root
    regions.ingest(name)
    regions.build(name, revision='r1')
    hints = bp.possible_yellow_overlap(state['source'], work/'tools'/'r1'/'mask.png')
    wf.save(root/'reviews'/f'{name}_possible_overlap.json', hints)
    return {'candidate': str(work/'tools'/'r1'), 'status': 'needs_agent_visual_selection_and_review',
            'hint_count': len(hints['candidates']), 'note':'No model was called; the supplied observations must be produced by the agent.'}


def gate(root, name, review_path):
    wf.safe_id(name)
    root = Path(root).resolve()
    work = root/'maps'/name
    s = wf.verified(work)
    review = wf.load(review_path)
    if review.get('candidate_sha256') != s['candidate_sha256']:
        raise ValueError('review is not tied to the current selected candidate')
    result = bp.review_gate(review)
    wf.save(root/'reviews'/f'{name}_review.json', review)
    wf.save(root/'reviews'/f'{name}_gate.json', result)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='command', required=True)
    a = sub.add_parser('prepare')
    for field in ('source','output','name'):
        a.add_argument('--'+field, required=True)
    a.add_argument('--tile', type=int, default=2048)
    a.add_argument('--overlap', type=int, default=128)
    b = sub.add_parser('gate')
    for field in ('root','name','review'):
        b.add_argument('--'+field, required=True)
    c = sub.add_parser('propose')
    for field in ('root','name','inventory','pairs'):
        c.add_argument('--'+field, required=True)
    d = sub.add_parser('finalize')
    for field in ('root','name','checklist'):
        d.add_argument('--'+field, required=True)
    e = sub.add_parser('select')
    for field in ('root','name','revision','evidence'):
        e.add_argument('--'+field, required=True)
    f = sub.add_parser('qa')
    for field in ('root','name'):
        f.add_argument('--'+field, required=True)
    g = sub.add_parser('build')
    for field in ('root','name','revision'):
        g.add_argument('--'+field, required=True)
    g.add_argument('--variant', choices=('tight','balanced','wide'), default='balanced')
    h = sub.add_parser('revise')
    for field in ('root','name','pairs','revision'):
        h.add_argument('--'+field, required=True)
    h.add_argument('--variant', choices=('tight','balanced','wide'), default='balanced')
    i = sub.add_parser('add-road')
    for field in ('root','name','discovery'):
        i.add_argument('--'+field, required=True)
    j = sub.add_parser('resolve')
    for field in ('root','name','decision'):
        j.add_argument('--'+field, required=True)
    args = p.parse_args()
    if args.command == 'prepare': result = prepare(args.source,args.output,args.name,args.tile,args.overlap)
    elif args.command == 'propose': result = propose(args.root,args.name,args.inventory,args.pairs)
    elif args.command == 'gate': result = gate(args.root,args.name,args.review)
    elif args.command == 'select':
        regions.ROOT = Path(args.root).resolve()
        wf.safe_id(args.name); wf.safe_id(args.revision)
        regions.select(args.name,args.revision,args.evidence)
        result = wf.status(regions.ROOT/'maps'/args.name)
    elif args.command == 'qa':
        regions.ROOT = Path(args.root).resolve()
        wf.safe_id(args.name)
        regions.qa(args.name)
        result = wf.status(regions.ROOT/'maps'/args.name)
    elif args.command == 'build':
        regions.ROOT = Path(args.root).resolve()
        wf.safe_id(args.name); wf.safe_id(args.revision)
        regions.build(args.name,args.variant,args.revision)
        result = {'candidate':str(regions.ROOT/'maps'/args.name/'tools'/args.revision), 'status':'needs_visual_selection'}
    elif args.command == 'revise':
        root=Path(args.root).resolve(); wf.safe_id(args.name); wf.safe_id(args.revision)
        work=root/'maps'/args.name; state=wf.verified(work)
        if state['phase'] not in ('tools','qa'): raise ValueError('revision requires tools or QA phase')
        if (work/'tools'/args.revision).exists(): raise FileExistsError('revision already exists')
        source_review=root/'reviews'/f'{args.name}_source.json'
        spec=wf.load(args.pairs)
        revised=bp.apply_pairs(wf.load(source_review),spec,state['size_wh'])
        wf.save(root/'reviews'/f'{args.name}_{args.revision}_pairs.json',spec)
        wf.save(source_review,revised)
        wf.save(root/'reviews'/f'{args.name}_{args.revision}_boundary_evidence.json',bp.boundary_evidence(state['source'],spec))
        regions.ROOT=root; regions.build(args.name,args.variant,args.revision)
        result={'candidate':str(work/'tools'/args.revision),'status':'needs_visual_selection'}
    elif args.command == 'add-road':
        root=Path(args.root).resolve(); wf.safe_id(args.name); work=root/'maps'/args.name
        state=wf.verified(work); discovery=wf.load(args.discovery); road=discovery['road']
        if state['phase'] not in ('qa','tools'): raise ValueError('road discovery requires tools or QA phase')
        wf.safe_id(road['id'])
        if any(r['id']==road['id'] for r in state['roads']): raise ValueError('road already exists')
        if not all(road.get(k) for k in ('id','evidence','continuation','polygon')) or not isinstance(road.get('obstacles'),list) or not isinstance(road.get('exits'),list): raise ValueError('road requires geometry and visual evidence')
        if len(road['polygon'])<3 or any(not (0<=p[0]<state['size_wh'][0] and 0<=p[1]<state['size_wh'][1]) for p in road['polygon']): raise ValueError('road polygon outside image or too short')
        tile_evidence=discovery.get('tile_evidence',{})
        known={t['id']:t for t in state['tiles']}
        if not tile_evidence or not set(tile_evidence).issubset(known) or any(not v for v in tile_evidence.values()): raise ValueError('source tile and visual evidence required')
        review_path=root/'reviews'/f'{args.name}_source.json'; observed=wf.load(review_path)
        observed['roads'].append(road)
        for tile_id in tile_evidence: observed['tiles'][tile_id]['roads'].append(road['id'])
        item={**road,'exits':[dict(x,status='pending') for x in road['exits']]}
        for x in item['exits']:
            if not x.get('evidence'): raise ValueError('each new exit requires evidence')
        state['roads'].append(item)
        for index, x in enumerate(item['exits']): state['unresolved'].append({'type':'inventory_exit','road_id':road['id'],'exit_index':index,'evidence':x['evidence']})
        state['unresolved'].append({'type':'other','severity':'major','location':road['id'],'candidate_before_discovery':state['candidate_sha256'],'evidence':'New road needs a rebuilt candidate and visual QA: '+road['evidence']})
        state.setdefault('road_discoveries',[]).append({'road_id':road['id'],'tile_evidence':tile_evidence,'prior_candidate_sha256':state['candidate_sha256']})
        wf.save(review_path,observed);wf.save(work/'workflow.json',state)
        result={'status':'road_added_rebuild_and_qa_required','road_id':road['id']}
    elif args.command == 'resolve':
        wf.safe_id(args.name)
        result=wf.resolve_issue(Path(args.root).resolve()/'maps'/args.name,wf.load(args.decision))
    else:
        wf.safe_id(args.name)
        result = wf.finalize(Path(args.root).resolve()/'maps'/args.name, wf.load(args.checklist))
    print(json.dumps(result, ensure_ascii=False, indent=2))
