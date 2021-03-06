from nose.tools import (
    eq_
)
from api.config import (
    Configuration,
    temp_config,
)
from core.analytics import Analytics
from api.google_analytics_provider import GoogleAnalyticsProvider
from . import DatabaseTest
from core.model import (
    get_one_or_create,
    CirculationEvent,
    DataSource,
    LicensePool
)
import urlparse
import datetime
from psycopg2.extras import NumericRange

class MockGoogleAnalyticsProvider(GoogleAnalyticsProvider):

    def post(self, url, params):
        self.count = self.count + 1 if hasattr(self, "count") else 1
        self.url = url
        self.params = params

class TestGoogleAnalyticsProvider(DatabaseTest):

    def test_from_config(self):        
        config = {
            Configuration.INTEGRATIONS: {
                GoogleAnalyticsProvider.INTEGRATION_NAME: {
                    "tracking_id": "faketrackingid"
                }
            }
        }        
        ga = GoogleAnalyticsProvider.from_config(config)
        eq_("faketrackingid", ga.tracking_id)

    def test_collect_event_with_work(self):
        ga = MockGoogleAnalyticsProvider("faketrackingid")
        work = self._work(
            title=u"pi\u00F1ata", authors=u"chlo\u00E9", fiction=True,
            audience="audience", language="lang", 
            with_license_pool=True, genre="Folklore"
        )
        work.presentation_edition.publisher = "publisher"
        work.target_age = NumericRange(10, 15)
        [lp] = work.license_pools
        now = datetime.datetime.utcnow()
        ga.collect_event(self._db, lp, CirculationEvent.CHECKIN, now)
        params = urlparse.parse_qs(ga.params)

        eq_(1, ga.count)
        eq_("http://www.google-analytics.com/collect", ga.url)
        eq_("faketrackingid", params['tid'][0])
        eq_("event", params['t'][0])
        eq_("circulation", params['ec'][0])
        eq_(CirculationEvent.CHECKIN, params['ea'][0])
        eq_(str(now), params['cd1'][0])
        eq_(lp.identifier.identifier, params['cd2'][0])
        eq_(lp.identifier.type, params['cd3'][0])
        eq_(u"pi\u00F1ata".encode('utf8'), params['cd4'][0])
        eq_(u"chlo\u00E9".encode('utf8'), params['cd5'][0])
        eq_("fiction", params['cd6'][0])
        eq_("audience", params['cd7'][0])
        eq_(work.target_age_string, params['cd8'][0])
        eq_("publisher", params['cd9'][0])
        eq_("lang", params['cd10'][0])
        eq_("Folklore", params['cd11'][0])

    def test_collect_event_without_work(self):
        ga = MockGoogleAnalyticsProvider("faketrackingid")

        identifier = self._identifier()
        source = DataSource.lookup(self._db, DataSource.GUTENBERG)
        pool, is_new = get_one_or_create(
            self._db, LicensePool, 
            identifier=identifier, data_source=source)

        now = datetime.datetime.utcnow()
        ga.collect_event(self._db, pool, CirculationEvent.CHECKIN, now)
        params = urlparse.parse_qs(ga.params)

        eq_(1, ga.count)
        eq_("http://www.google-analytics.com/collect", ga.url)
        eq_("faketrackingid", params['tid'][0])
        eq_("event", params['t'][0])
        eq_("circulation", params['ec'][0])
        eq_(CirculationEvent.CHECKIN, params['ea'][0])
        eq_(str(now), params['cd1'][0])
        eq_(pool.identifier.identifier, params['cd2'][0])
        eq_(pool.identifier.type, params['cd3'][0])
        eq_(None, params.get('cd4'))
        eq_(None, params.get('cd5'))
        eq_(None, params.get('cd6'))
        eq_(None, params.get('cd7'))
        eq_(None, params.get('cd8'))
        eq_(None, params.get('cd9'))
        eq_(None, params.get('cd10'))
        eq_(None, params.get('cd11'))
