import json
import pytest
import test_live_editor as fixtures
from street_story.research_runs import begin_research_run, register_discovered_source

from street_story import live as proposed

URL='https://de.wikipedia.org/wiki/Sackheimer_Tor'

def setup(tmp_path,*,stale=None,registered=True):
 svc,adapter,session,_=fixtures.make_service(tmp_path)
 with svc.store.tx() as db:
  row=db.execute('SELECT * FROM stories WHERE id=?',(session.resource_id,)).fetchone()
  identity={'status':'match','visual_reference_verified':True,'photo_sha256':row['photo_sha256'] if stale!='photo' else 'other-photo','generation':0 if stale!='generation' else 1,'candidate_id':'wiki:77','candidate_name':'Gate','reference_evidence':[{'article_url':URL,'article_source_sha256':'a'*64}]}
  db.execute('UPDATE stories SET research_json=? WHERE id=?',(json.dumps({'visual_identity':identity,'identity_generation':0}),session.resource_id))
  run=begin_research_run(db,story_id=session.resource_id,poi_key='wiki:77',goal='Read known material',scope='saved-identity-article',expected_story_revision=row['revision'],identity_generation=1 if stale=='run' else 0,run_id='research_current',now=svc.store.now())
  if registered:
   register_discovered_source(db,run_id=run,url=URL,title='Sackheimer Tor',status='discovered',now=svc.store.now())
 return svc,adapter,session

def test_saved_identity_article_is_addressable_via_normal_context(tmp_path):
 svc,adapter,session=setup(tmp_path)
 state=adapter._topic_state(session.resource_id)
 catalog=adapter._compact_context(state)['research_run']['identity_article_sources']
 assert catalog==[{'source_ref':proposed._search_source_ref(URL),'url':URL,'title':'Sackheimer Tor','status':'discovered','source_version_id':None,'identity_article_source_sha256':'a'*64}]
 # A normal model can choose get_research_chunk from server-owned context,
 # without fresh owner URL injection or a search call.
 initialized=adapter.initialize(resource_id=session.resource_id,actor=None,model='gemini-3.8-live')
 assert initialized['context']['research_run']['identity_article_sources']==catalog
 with svc.store.connection() as db:
  assert db.execute('SELECT count(*) FROM source_versions').fetchone()[0]==0
  assert db.execute('SELECT count(*) FROM fact_observations').fetchone()[0]==0

@pytest.mark.parametrize('stale',['photo','generation','run'])
def test_stale_identity_article_is_not_projected_into_current_scope(tmp_path,stale):
 _,adapter,session=setup(tmp_path,stale=stale)
 assert adapter._topic_state(session.resource_id)['research_run']['identity_article_sources']==[]

def test_reference_without_current_run_discovery_is_not_addressable(tmp_path):
 _,adapter,session=setup(tmp_path,registered=False)
 assert adapter._topic_state(session.resource_id)['research_run']['identity_article_sources']==[]
