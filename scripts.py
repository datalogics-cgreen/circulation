from cStringIO import StringIO
from datetime import timedelta
from nose.tools import set_trace
import csv
import json
import os
import sys
import time
import urlparse
import logging
import argparse

from sqlalchemy import (
    or_,
    func,
)
from sqlalchemy.orm import (
    contains_eager, 
    defer
)
from psycopg2.extras import NumericRange

from api.lanes import make_lanes
from api.controller import CirculationManager
from api.monitor import SearchIndexMonitor
from api.threem import ThreeMCirculationSweep
from api.overdrive import OverdriveAPI
from core import log
from core.lane import Lane
from core.classifier import Classifier
from core.model import (
    Contribution,
    CustomList,
    DataSource,
    DeliveryMechanism,
    Edition,
    Hyperlink,
    Identifier,
    LicensePool,
    LicensePoolDeliveryMechanism,
    Representation,
    Subject,
    Work,
)
from core.scripts import (
    Script as CoreScript,
    RunCoverageProvidersScript,
    RunCoverageProviderScript,
    IdentifierInputScript,
    RunMonitorScript,
)
from core.lane import (
    Pagination,
    Facets,
)
from api.config import Configuration
from core.opds_import import (
    SimplifiedOPDSLookup,
    OPDSImporter,
)
from core.opds import (
    AcquisitionFeed,
)
from core.util.opds_writer import (    
     OPDSFeed,
)
from core.external_list import CustomListFromCSV
from core.external_search import ExternalSearchIndex
from core.util import LanguageCodes
from api.opds import CirculationManagerAnnotator

from api.circulation import CirculationAPI
from api.overdrive import (
    OverdriveAPI,
    OverdriveBibliographicCoverageProvider,
)
from api.threem import (
    ThreeMAPI,
    ThreeMBibliographicCoverageProvider,
)
from api.axis import (
    Axis360API,
)
from core.axis import Axis360BibliographicCoverageProvider

class Script(CoreScript):
    def load_config(self):
        if not Configuration.instance:
            Configuration.load()

class CreateWorksForIdentifiersScript(Script):

    """Do the bare minimum to associate each Identifier with an Edition
    with title and author, so that we can calculate a permanent work
    ID.
    """
    to_check = [Identifier.OVERDRIVE_ID, Identifier.THREEM_ID,
                Identifier.GUTENBERG_ID]
    BATCH_SIZE = 100
    name = "Create works for identifiers"

    def __init__(self, metadata_web_app_url=None):
        self.metadata_url = (
            metadata_web_app_url or Configuration.integration_url(
                Configuration.METADATA_WRANGLER_INTEGRATION
            )
        )
        self.lookup = SimplifiedOPDSLookup(self.metadata_url)

    def run(self):

        # We will try to fill in Editions that are missing
        # title/author and as such have no permanent work ID.
        #
        # We will also try to create Editions for Identifiers that
        # have no Edition.

        either_title_or_author_missing = or_(
            Edition.title == None,
            Edition.sort_author == None,
        )
        edition_missing_title_or_author = self._db.query(Identifier).join(
            Identifier.primarily_identifies).filter(
                either_title_or_author_missing)

        no_edition = self._db.query(Identifier).filter(
            Identifier.primarily_identifies==None).filter(
                Identifier.type.in_(self.to_check))

        for q, descr in (
                (edition_missing_title_or_author,
                 "identifiers whose edition is missing title or author"),
                (no_edition, "identifiers with no edition")):
            batch = []
            self.log.debug("Trying to fix %d %s", q.count(), descr)
            for i in q:
                batch.append(i)
                if len(batch) >= self.BATCH_SIZE:
                    self.process_batch(batch)
                    batch = []

    def process_batch(self, batch):
        response = self.lookup.lookup(batch)

        if response.status_code != 200:
            raise Exception(response.text)
            
        content_type = response.headers['content-type']
        if content_type != OPDSFeed.ACQUISITION_FEED_TYPE:
            raise Exception("Wrong media type: %s" % content_type)

        importer = OPDSImporter(
            self._db, response.text,
            overwrite_rels=[Hyperlink.DESCRIPTION, Hyperlink.IMAGE])
        imported, messages_by_id = importer.import_from_feed()
        self.log.info("%d successes, %d failures.", 
                      len(imported), len(messages_by_id))
        self._db.commit()

class MetadataCalculationScript(Script):

    """Force calculate_presentation() to be called on some set of Editions.

    This assumes that the metadata is in already in the database and
    will fall into place if we just call
    Edition.calculate_presentation() and Edition.calculate_work() and
    Work.calculate_presentation().

    Most of these will be data repair scripts that do not need to be run
    regularly.

    """

    name = "Metadata calculation script"

    def q(self):
        raise NotImplementedError()

    def run(self):
        q = self.q()
        search_index_client = ExternalSearchIndex()
        self.log.info("Attempting to repair metadata for %d works" % q.count())

        success = 0
        failure = 0
        also_created_work = 0

        def checkpoint():
            self._db.commit()
            self.log.info("%d successes, %d failures, %d new works.",
                          success, failure, also_created_work)

        i = 0
        for edition in q:
            edition.calculate_presentation()
            if edition.sort_author:
                success += 1
                work, is_new = edition.license_pool.calculate_work(
                    search_index_client=search_index_client)
                if work:
                    work.calculate_presentation()
                    if is_new:
                        also_created_work += 1
            else:
                failure += 1
            i += 1
            if not i % 1000:
                checkpoint()
        checkpoint()

class FillInAuthorScript(MetadataCalculationScript):
    """Fill in Edition.sort_author for Editions that have a list of
    Contributors, but no .sort_author.

    This is a data repair script that should not need to be run
    regularly.
    """

    name = "Fill in missing authors"

    def q(self):
        return self._db.query(Edition).join(
            Edition.contributions).join(Contribution.contributor).filter(
                Edition.sort_author==None)

class UpdateStaffPicksScript(Script):

    DEFAULT_URL_TEMPLATE = "https://docs.google.com/spreadsheets/d/%s/export?format=csv"

    def run(self):
        inp = self.open()
        tag_fields = {
            'tags': Subject.NYPL_APPEAL,
        }

        integ = Configuration.integration(Configuration.STAFF_PICKS_INTEGRATION)
        fields = integ.get(Configuration.LIST_FIELDS, {})

        importer = CustomListFromCSV(
            DataSource.LIBRARY_STAFF, CustomList.STAFF_PICKS_NAME,
            **fields
        )
        reader = csv.DictReader(inp, dialect='excel-tab')
        importer.to_customlist(self._db, reader)
        self._db.commit()

    def open(self):
        if len(sys.argv) > 1:
            return open(sys.argv[1])

        url = Configuration.integration_url(
            Configuration.STAFF_PICKS_INTEGRATION, True
        )
        if not url.startswith('https://') or url.startswith('http://'):
            url = self.DEFAULT_URL_TEMPLATE % url
        self.log.info("Retrieving %s", url)
        representation, cached = Representation.get(
            self._db, url, do_get=Representation.browser_http_get,
            accept="text/csv", max_age=timedelta(days=1))
        if representation.status_code != 200:
            raise ValueError("Unexpected status code %s" % 
                             representation.status_code)
        if not representation.media_type.startswith("text/csv"):
            raise ValueError("Unexpected media type %s" % 
                             representation.media_type)
        return StringIO(representation.content)

class LaneSweeperScript(Script):
    """Do something to each lane in the application."""

    def __init__(self):
        os.environ['AUTOINITIALIZE'] = "False"
        from api.app import app
        del os.environ['AUTOINITIALIZE']
        app.manager = CirculationManager(self._db)
        self.app = app
        self.base_url = Configuration.integration_url(
            Configuration.CIRCULATION_MANAGER_INTEGRATION, required=True
        )

    def run(self):
        begin = time.time()
        client = self.app.test_client()
        ctx = self.app.test_request_context(base_url=self.base_url)
        ctx.push()
        queue = [self.app.manager.top_level_lane]
        while queue:
            new_queue = []
            self.log.debug("Beginning of loop: %d lanes to process", len(queue))
            for l in queue:
                if self.should_process_lane(l):
                    self.process_lane(l)
                    self._db.commit()
                for sublane in l.sublanes:
                    new_queue.append(sublane)
            queue = new_queue
        ctx.pop()
        end = time.time()
        self.log.info("Entire process took %.2fsec", (end-begin))

    def should_process_lane(self, lane):
        return True

    def process_lane(self, lane):
        pass

class CacheRepresentationPerLane(LaneSweeperScript):

    name = "Cache one representation per lane"

    def __init__(self, max_depth=None):
        self.max_depth = max_depth
        super(CacheRepresentationPerLane, self).__init__()

    def should_process_lane(self, lane):
        if lane.name is None:
            return False
            
        if lane.parent is None and not isinstance(lane, Lane):
            return False

        if self.max_depth and lane.depth > self.max_depth:
            return False

        return True

    def cache_url(self, annotator, lane, languages):
        raise NotImplementedError()

    def generate_representation(self, *args, **kwargs):
        raise NotImplementedError()

    # The generated document will probably be an OPDS acquisition
    # feed.
    ACCEPT_HEADER = OPDSFeed.ACQUISITION_FEED_TYPE

    cache_url_method = None

    def process_lane(self, lane):
        annotator = self.app.manager.annotator(lane)
        a = time.time()
        lane_key = "%s/%s" % (lane.language_key, lane.name)
        self.log.info(
            "Generating feed(s) for %s", lane_key
        )
        cached_feeds = self.do_generate(lane)
        b = time.time()
        if not isinstance(cached_feeds, list):
            cached_feeds = [cached_feeds]
        total_size = sum(len(x.content) for x in cached_feeds if x)
        self.log.info(
            "Generated %d feed(s) for %s. Took %.2fsec to make %d bytes.",
            len(cached_feeds), lane_key, (b-a), total_size
        )

class CacheFacetListsPerLane(CacheRepresentationPerLane):
    """Cache the first two pages of every facet list for this lane."""

    name = "Cache first two pages of every facet list for each lane"

    def do_generate(self, lane):
        feeds = []
        annotator = self.app.manager.annotator(lane)
        if isinstance(lane, Lane):
            languages = lane.language_key
            lane_name = None
        else:
            languages = None
            lane_name = None

        url = self.app.manager.cdn_url_for(
            "feed", languages=lane.languages, lane_name=lane_name
        )

        order_facets = Configuration.enabled_facets(
            Facets.ORDER_FACET_GROUP_NAME
        )
        availability = Configuration.default_facet(
            Facets.AVAILABILITY_FACET_GROUP_NAME
        )
        collection = Configuration.default_facet(
            Facets.COLLECTION_FACET_GROUP_NAME
        )        

        for sort_order in order_facets:
            pagination = Pagination.default()
            facets = Facets(
                collection=collection, availability=availability,
                order=sort_order, order_ascending=True
            )
            title = lane.display_name
            for pagenum in (0, 2):
                feeds.append(
                    AcquisitionFeed.page(
                        self._db, title, url, lane, annotator, 
                        facets=facets, pagination=pagination,
                        force_refresh=True
                    )
                )
                pagination = pagination.next_page
        return feeds

class CacheOPDSGroupFeedPerLane(CacheRepresentationPerLane):

    name = "Cache OPDS group feed for each lane"

    def should_process_lane(self, lane):
        # OPDS group feeds are only generated for lanes that have sublanes.
        if not lane.sublanes:
            return False
        if self.max_depth and lane.depth > self.max_depth:
            return False
        return True

    def do_generate(self, lane):
        feeds = []
        annotator = self.app.manager.annotator(lane)
        title = lane.display_name
        if isinstance(lane, Lane):
            languages = lane.language_key
            lane_name = lane.name
        else:
            languages = None
            lane_name = None
        url = self.app.manager.cdn_url_for(
            "acquisition_groups", languages=languages, lane_name=lane_name
        )
        return AcquisitionFeed.groups(
            self._db, title, url, lane, annotator,
            force_refresh=True
        )


class BibliographicCoverageProvidersScript(RunCoverageProvidersScript):
    """Alternate between running bibliographic coverage providers for
    all registered book sources.
    """

    def __init__(self):

        providers = []
        if Configuration.integration('3M'):
            providers.append(ThreeMBibliographicCoverageProvider)
        if Configuration.integration('Overdrive'):
            providers.append(OverdriveBibliographicCoverageProvider)
        if Configuration.integration('Axis 360'):
            providers.append(Axis360BibliographicCoverageProvider)

        if not providers:
            raise Exception("No licensed book sources configured, nothing to get coverage from!")
        super(BibliographicCoverageProvidersScript, self).__init__(providers)


class AvailabilityRefreshScript(IdentifierInputScript):
    """Refresh the availability information for a LicensePool, direct from the
    license source.
    """
    def do_run(self):
        args = self.parse_command_line(self._db)
        if not args.identifiers:
            raise Exception(
                "You must specify at least one identifier to refresh."
            )
        
        # We don't know exactly how big to make these batches, but 10 is
        # always safe.
        start = 0
        size = 10
        while start < len(args.identifiers):
            batch = args.identifiers[start:start+size]
            self.refresh_availability(batch)
            self._db.commit()
            start += size

    def refresh_availability(self, identifiers):
        provider = None
        identifier = identifiers[0]
        if identifier.type==Identifier.THREEM_ID:
            sweeper = ThreeMCirculationSweep(self._db)
            sweeper.process_batch(identifiers)
        elif identifier.type==Identifier.OVERDRIVE_ID:
            api = OverdriveAPI(self._db)
            for identifier in identifiers:
                api.update_licensepool(identifier.identifier)
        elif identifier.type==Identifier.AXIS_360_ID:
            provider = Axis360BibliographicCoverageProvider(self._db)
            provider.process_batch(identifiers)
        else:
            self.log.warn("Cannot update coverage for %r" % identifier.type)

class LanguageListScript(Script):
    """List all the languages with at least one non-open access work
    in the collection.
    """

    def do_run(self):

        query = self._db.query(Edition.language, func.count(Edition.language)).group_by(Edition.language)
        query = query.join(Edition.primary_identifier).join(
            Identifier.licensed_through
        ).join(LicensePool.delivery_mechanisms).join(
            LicensePoolDeliveryMechanism.delivery_mechanism
        )

        # TODO: It would be more reliable to use
        # Lane.only_show_ready_deliverable_works here, but that's
        # geared towards operating on Work. It's not a big deal since
        # this is just to get a general count.

        query = query.filter(LicensePool.open_access==False).filter(
            LicensePool.licenses_owned > 0
        ).filter(
            Edition.medium==Edition.BOOK_MEDIUM
        ).filter(
            Edition.language != None
        ).filter(
            DeliveryMechanism.default_client_can_fulfill==True
        )
        name = LanguageCodes.name_for_languageset
        sorted_languages = sorted(
            query.all(), key=lambda x: (
                -x[1], name(x[0])
            )
        )
        sorted_languages = [
            (language, count, name(language))
            for (language, count) in sorted_languages
        ]

        print "\n".join(["%s %i (%s)" % l for l in sorted_languages])
        print json.dumps([l[0] for l in sorted_languages])

class CompileTranslationsScript(Script):
    """A script to combine translation files for circulation, core
    and the admin interface, and compile the result to be used by the
    app. The combination step is necessary because Flask-Babel does not
    support multiple domains yet.
    """

    def run(self):
        languages = Configuration.localization_languages()
        for language in languages:
            base_path = "translations/%s/LC_MESSAGES" % language
            if not os.path.exists(base_path):
                logging.warn("No translations for configured language %s" % language)
                continue

            os.system("rm %(path)s/messages.po" % dict(path=base_path))
            os.system("cat %(path)s/*.po > %(path)s/messages.po" % dict(path=base_path))
        
        os.system("pybabel compile -f -d translations")

class UpdateSearchIndexScript(RunMonitorScript):

    def __init__(self):
        parser = argparse.ArgumentParser()
        parser.add_argument(
            '--works-index', 
            help='The ElasticSearch index to update, if other than the default.'
        )
        parsed = parser.parse_args()

        super(UpdateSearchIndexScript, self).__init__(
            SearchIndexMonitor,
            index_name=parsed.works_index,
        )
