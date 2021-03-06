# -*- coding: utf-8 -*-
from collections import namedtuple

import pytest

from cfme.configure import settings
from cfme.containers.overview import match_page as match_page_containersoverview
from cfme.containers.node import match_page as match_page_node
from cfme.containers.pod import match_page as match_page_pod
from cfme.containers.service import match_page as match_page_service
from cfme.containers.container import match_page as match_page_container
from cfme.containers.provider import ContainersProvider, \
    match_page as match_page_containersprovider
from cfme.web_ui import browser_title
from utils import testgen
from utils.appliance.implementations.ui import navigate_to
from utils.version import current_version
from utils.blockers import BZ


pytestmark = [pytest.mark.uncollectif(lambda: current_version() < "5.8")]
pytest_generate_tests = testgen.generate([ContainersProvider], scope='function')


DataSet = namedtuple('DataSet', ['match_page', 'start_page_name'])
data_sets = (
    DataSet(match_page_containersoverview, 'Containers / Overview'),
    DataSet(match_page_containersprovider, 'Containers / Providers'),
    DataSet(match_page_node, 'Containers / Nodes'),
    DataSet(match_page_pod, 'Containers / Pods'),
    DataSet(match_page_service, 'Containers / Services'),
    DataSet(match_page_container, 'Containers / Explorer')
)


@pytest.mark.meta(blockers=[BZ(1446265, forced_streams=["5.8", "upstream"])])
@pytest.mark.polarion('CMP-10601')
def test_start_page(appliance, soft_assert):

    for data_set in data_sets:
        settings.visual.login_page = data_set.start_page_name
        login_page = navigate_to(appliance.server, 'LoginScreen')
        login_page.login_admin()
        soft_assert(
            data_set.match_page(),
            'Configured start page is "{}", but the start page now'
            ' is "{}" instead of "{}"'.format(
                data_set.start_page_name, data_set.match_page.keywords['title'],
                browser_title()
            )
        )
