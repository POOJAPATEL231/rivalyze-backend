import os
os.environ['MOCK_MODE']='1'
from app.core import orchestrator, merge, confidence
from app.agents import news, product, review, strategist, discovery
from app.models import Competitor
agents = {
  'discovery': orchestrator.discovery_node(discovery.run),
  'news': orchestrator.gather_node('news', news.run),
  'product': orchestrator.gather_node('product', product.run),
  'review': orchestrator.gather_node('review', review.run),
  'merge': merge.merge_node,
  'strategist': orchestrator.strategist_node(strategist.run, confidence.confidence),
}
evs=[]
def emit(a,m): evs.append((a,m))
state={'run_id':'test-run','company':'Notion','competitors':[Competitor(name='ClickUp',category='direct',rationale='x'),Competitor(name='Coda',category='direct',rationale='y')]}
final=orchestrator.run_analysis(state, agents, emit)
rep=final.get('report')
print('REPORT TYPE:', type(rep).__name__)
print('THREAT:', getattr(rep,'threat_level',None))
print('SUMMARY:', getattr(rep,'executive_summary','')[:80])
print('RECS:', [(r.action[:30], r.confidence, r.evidence_ids) for r in getattr(rep,'recommendations',[])])
print('H2H rivals:', [list(row.rivals.keys()) for row in getattr(rep,'head_to_head',[])])
print('unified signals:', len(final.get('unified').signals) if final.get('unified') else 'none')
