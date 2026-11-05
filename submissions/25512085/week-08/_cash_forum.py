"""Internal availability-only notice planning; publishing remains with callers."""


def availability_plan(policy, units, hub, catalog, now, forum=None):
    """Match real recent requests to certified stock; publish availability only."""
    state=hub.get('forum_state',{})
    if now-state.get('last_shop_post_ts',0)<300:
        return None
    held={u['sku']:u for u in units if u.get('sku') in catalog and u.get('cert')
          and u.get('grade')!='expired' and (u.get('kind')!='food' or (u.get('expires_at') or 0)>now+600)}
    if not held:
        return None
    lookup={s.lower():s for s in held}
    def text_for(skus):
        lines=['[mjforge 재고 안내]']
        for sku in skus[:3]:
            name=str(catalog[sku].get('name') or held[sku].get('name') or sku).replace('\n',' ').replace('\r',' ')[:100]
            lines.append(f'{sku} {name}: 재고 있음')
        return '\n'.join(lines)
    posts=forum.get('posts',[]) if isinstance(forum,dict) else forum if isinstance(forum,list) else []
    replied=set(state.get('availability_replied_threads',[]))
    request=policy.re.compile(r'looking for|want to buy|stock request|(?:check\s+)?availability\s*(?:of|\?)|in stock\?|구매\s*(?:희망|원|요청)|재고\s*(?:있|문의)|찾(?:아요|습니다|고 있어)',policy.re.I)
    report=policy.re.compile(r'\bsettled\b|\bpaid\b|\bpurchased\b|\bbought\b|not a guarantee|정산|구매\s*(?:완료|후기)|거래\s*(?:완료|후기)|리포트',policy.re.I)
    for post in sorted(posts,key=lambda x:x.get('ts',0),reverse=True):
        pid=post.get('id');body=str(post.get('text') or '')
        if (post.get('role')!='buyer' or str(post.get('owner') or '').lower()=='mjforge'
                or not policy.number(post.get('ts')) or not 0<=now-post['ts']<=1200
                or pid is None or str(pid) in replied or report.search(body) or not request.search(body)):
            continue
        skus=sorted({lookup[k.lower()] for k in policy.re.findall(r'\b[GgFf][0-9a-fA-F]{7,10}\b',body) if k.lower() in lookup})
        if skus:
            return {'action':{'post':text_for(skus),'refs':[],'as':'shop','thread':pid},
                    'skus':skus[:3],'demand_ts':post['ts'],'thread':str(pid)}
    if now-state.get('last_shop_post_ts',0)<policy.config().get('forum_interval_seconds',1200):
        return None
    announced=state.get('availability_announced',{})
    wants=hub.get('demand_radar',{}).get('sku_inquiries',{})
    ranked=[]
    for sku in held:
        events=[e['ts'] for e in wants.get(sku.lower(),[]) if policy.number(e.get('ts')) and 0<=now-e['ts']<=1200
                and e['ts']>announced.get(sku,0)]
        if events:ranked.append((len(events),max(events),sku))
    if not ranked:
        return None
    chosen=sorted(ranked,reverse=True)[:3];skus=[v[2] for v in chosen]
    return {'action':{'post':text_for(skus),'refs':[],'as':'shop'},'skus':skus,
            'demand_ts':max(v[1] for v in chosen),'thread':None}
