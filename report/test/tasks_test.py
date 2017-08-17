import unittest
from google.appengine.ext import testbed
import mock

from cloudstorage import cloudstorage_api  # stubbed by testbed
from report.tasks import _generate_site

_FAKE_DRC_SHARE_BUCKET = 'fake-drc'

class TasksTest(unittest.TestCase):
    def setUp(self):
        super(ReportTest, self).setUp()
        self.testbed = testbed.Testbed()
        self.testbed.activate()
        self.testbed.init_app_identity_stub()
        self.testbed.init_memcache_stub()
        self.testbed.init_urlfetch_stub()
        self.testbed.init_blobstore_stub()
        self.testbed.init_datastore_v3_stub()

    @mock.patch('report.api_util.check_cron')
    def test_report_check_cron(self, mock_check_cron):
        output = _generate_site()
        self.assertEquals(mock_check_cron.call_count, 1)

    def test_site_generation(self):
        # generating site
        self.assertEquals(_generate_site(_FAKE_DRC_SHARE_BUCKET),'okay')

        # verify that page worked
        bucket = _FAKE_DRC_SHARE_BUCKET
        file_count = 0
        for stat in cloudstorage.listbucket(bucket + '/', delimiter='/'):
            assert(not stat.is_dir)
            assert(stat.filename in \
                    [name + '.html' for name in ['report','data_model','file_transfer','index']])
            file_count += 1
        self.assertEquals(file_count,4)

    def _write_cloud_csv(self, file_name, contents_str):
        with cloudstorage_api.open('/%s/%s' % (_FAKE_BUCKET, file_name), mode='w') as cloud_file:
            cloud_file.write(contents_str.encode('utf-8'))

    def tearDown(self):
        self.testbed.deactivate()
