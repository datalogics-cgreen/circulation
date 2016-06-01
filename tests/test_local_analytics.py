from nose.tools import (
    eq_,
)
from api.local_analytics import LocalAnalytics
from . import DatabaseTest
from core.model import CirculationEvent
import datetime

class TestLocalAnalytics(DatabaseTest):

    def test_collect(self):
        la = LocalAnalytics()
        work = self._work(
            title="title", authors="author", fiction=True,
            audience="audience", language="lang",
            with_license_pool=True
        )
        [lp] = work.license_pools
        now = datetime.datetime.utcnow()
        la.collect(
            self._db, lp, CirculationEvent.CHECKIN, now, 
            old_value=None, new_value=None)
        [event] = self._db \
            .query(CirculationEvent) \
            .filter(CirculationEvent.type == CirculationEvent.CHECKIN) \
            .all()

        eq_(lp, event.license_pool)
        eq_(CirculationEvent.CHECKIN, event.type)
        eq_(now, event.start)