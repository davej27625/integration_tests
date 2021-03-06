import logging
import os
import re
import socket
import traceback
from copy import copy
from tempfile import NamedTemporaryFile
from textwrap import dedent
from time import sleep
from urlparse import ParseResult, urlparse

import dateutil.parser
import fauxfactory
import requests
import yaml
from cached_property import cached_property
from manageiq_client.api import ManageIQClient as MiqApi
from sentaku import ImplementationContext
from werkzeug.local import LocalStack, LocalProxy

from fixtures import ui_coverage
from fixtures.pytest_store import store
from utils import clear_property_cache
from utils import conf, datafile, db, ssh, ports
from utils.datafile import load_data_file
from utils.events import EventListener
from utils.log import logger, create_sublogger, logger_wrap
from utils.net import net_check, resolve_hostname
from utils.path import data_path, patches_path, scripts_path, conf_path
from utils.version import Version, get_stream, pick, LATEST
from utils.wait import wait_for
from .implementations.ui import ViaUI

RUNNING_UNDER_SPROUT = os.environ.get("RUNNING_UNDER_SPROUT", "false") != "false"

# EMS types recognized by IP or credentials
RECOGNIZED_BY_IP = [
    "InfraManager", "ContainerManager", "MiddlewareManager", "Openstack::CloudManager"]
RECOGNIZED_BY_CREDS = ["CloudManager"]

# A helper for the IDs
SEQ_FACT = 1e12


def _current_miqqe_version():
    """Parses MiqQE JS patch version from the patch file

    Returns: Version as int
    """
    with patches_path.join('miq_application.js.diff').open("r") as f:
        match = re.search("MiqQE_version = (\d+);", f.read(), flags=0)
    version = int(match.group(1))
    return version


current_miqqe_version = _current_miqqe_version()


class ApplianceException(Exception):
    pass


class ApplianceConsole(object):
    """ApplianceConsole is used for navigating and running appliance_console commands against an
    appliance."""

    def __init__(self, appliance):
        self.appliance = appliance

    def run_commands(self, commands, autoreturn=True, timeout=20, channel=None):
        if not channel:
            channel = self.appliance.ssh_client.invoke_shell()
        self.commands = commands
        for command in commands:
            if isinstance(command, basestring):
                command_string, timeout = command, timeout
            else:
                command_string, timeout = command
            channel.settimeout(timeout)
            if autoreturn:
                command_string = (command_string + '\n')
            channel.send("{}".format(command_string))
            result = ''
            try:
                while True:
                    result += channel.recv(1)
                    if 'Press any key to continue' in result:
                        break
            except socket.timeout:
                pass
            logger.debug(result)


class ApplianceConsoleCli(object):

    def __init__(self, appliance):
        self.appliance = appliance

    def _run(self, appliance_console_cli_command):
        return self.appliance.ssh_client.run_command(
            "appliance_console_cli {}".format(appliance_console_cli_command))

    def set_hostname(self, hostname):
        self._run("--host {host}".format(host=hostname))

    def configure_appliance_external_join(self, dbhostname,
            username, password, dbname, fetch_key, sshlogin, sshpass):
        self._run("--hostname {dbhostname} --username {username} --password {password}"
            " --dbname {dbname} --verbose --fetch-key {fetch_key} --sshlogin {sshlogin}"
            " --sshpassword {sshpass}".format(dbhostname=dbhostname, username=username,
                password=password, dbname=dbname, fetch_key=fetch_key, sshlogin=sshlogin,
                sshpass=sshpass))

    def configure_appliance_external_create(self, region, dbhostname,
            username, password, dbname, fetch_key, sshlogin, sshpass):
        self._run("--region {region} --hostname {dbhostname} --username {username}"
            " --password {password} --dbname {dbname} --verbose --fetch-key {fetch_key}"
            " --sshlogin {sshlogin} --sshpassword {sshpass}".format(
                region=region, dbhostname=dbhostname, username=username, password=password,
                dbname=dbname, fetch_key=fetch_key, sshlogin=sshlogin, sshpass=sshpass))

    def configure_appliance_internal_fetch_key(self, region, dbhostname,
            username, password, dbname, fetch_key, sshlogin, sshpass):
        self._run("--region {region} --internal --hostname {dbhostname} --username {username}"
            " --password {password} --dbname {dbname} --verbose --fetch-key {fetch_key}"
            " --sshlogin {sshlogin} --sshpassword {sshpass}".format(
                region=region, dbhostname=dbhostname, username=username, password=password,
                dbname=dbname, fetch_key=fetch_key, sshlogin=sshlogin, sshpass=sshpass))

    def configure_ipa(self, ipaserver, username, password, domain, realm):
        self._run("--ipaserver {ipaserver} --ipaprincipal {username} --ipapassword {password}"
            " --ipadomain {domain} --iparealm {realm}".format(
                ipaserver=ipaserver, username=username, password=password, domain=domain,
                realm=realm))
        assert self.appliance.ssh_client.run_command("systemctl status sssd | grep running")
        return_code, output = self.appliance.ssh_client.run_command(
            "cat /etc/ipa/default.conf | grep 'enable_ra = True'")
        assert return_code == 0

    def uninstall_ipa_client(self):
        self._run("--uninstall-ipa")
        return_code, output = self.appliance.ssh_client.run_command(
            "cat /etc/ipa/default.conf")
        assert return_code != 0


class IPAppliance(object):
    """IPAppliance represents an already provisioned cfme appliance whos provider is unknown
    but who has an IP address. This has a lot of core functionality that Appliance uses, since
    it knows both the provider, vm_name and can there for derive the IP address.

    Args:
        ipaddress: The IP address of the provider
        browser_steal: If True then then current browser is killed and the new appliance
            is used to generate a new session.
        container: If the appliance is running as a container or as a pod, specifies its name.
        openshift_creds: If the appliance runs as a project on openshift, provides credentials for
            the openshift host so the framework can interact with the project.
        db_host: If the database is located somewhere else than on the appliance itself, specify
            the host here.
    """
    _nav_steps = {}

    def __init__(
            self, address=None, browser_steal=False, container=None, openshift_creds=None,
            db_host=None):
        if address is not None:
            if not isinstance(address, ParseResult):
                address = urlparse(str(address))
            if not (address.scheme and address.netloc):
                # Use .path (w.x.y.z ip format)
                self.address = address.path
                self.scheme = "https"
                self._url = "https://{}/".format(address.path)
            else:
                # schema://w.x.y.z/ format
                self.address = address.netloc
                self.scheme = address.scheme
                self._url = address.geturl()
        self.browser_steal = browser_steal
        self.container = container
        self.openshift_creds = openshift_creds or {}
        self.db_host = db_host
        self._db_ssh_client = None
        self._user = None
        self.appliance_console = ApplianceConsole(self)
        self.appliance_console_cli = ApplianceConsoleCli(self)
        self.browser = ViaUI(owner=self)
        self.context = ImplementationContext.from_instances(
            [self.browser])
        self._server = None
        self.is_pod = False

    def get(self, cls, *args, **kwargs):
        """A generic getter for instantiation of Collection classes

        This generic getter will supply an appliance (self) to an object and instantiate
        it with the supplied args/kwargs e.g.::

          my_appliance.get(NodeCollection)

        This will return a NodeCollection object that is bound to the appliance.
        """
        assert 'appliance' not in kwargs
        return cls(appliance=self, *args, **kwargs)

    @property
    def postgres_version(self):
        # postgres's version is in the service name and file paths when we pull it from SCL,
        # so this is a little resolver to help keep the version picking centralized
        return 'rh-postgresql95'

    @property
    def default_zone(self):
        from cfme.base import Region, Zone
        return Zone(self, region=Region(self, self.server_region()))

    @property
    def server(self):
        if self._server is None:
            from cfme.base import Server
            self._server = Server(appliance=self, zone=self.default_zone, sid=self.server_id())
        return self._server

    @property
    def user(self):
        from cfme.configure.access_control import User
        from cfme.base.credential import Credential
        if self._user is None:
            # Admin by default
            username = conf.credentials['default']['username']
            password = conf.credentials['default']['password']
            logger.info(
                '%r.user was set to None before, therefore generating an admin user: %s/%s',
                self, username, password)
            cred = Credential(principal=username, secret=password)
            self._user = User(credential=cred, appliance=self, name='Administrator')
        return self._user

    @user.setter
    def user(self, user_object):
        if user_object is None:
            logger.info('%r.user set to None, will be set to admin on next access', self)
        self._user = user_object

    @property
    def appliance(self):
        return self

    def __repr__(self):
        return '{}({})'.format(type(self).__name__, repr(self.address))

    def __call__(self, **kwargs):
        """Syntactic sugar for overriding certain instance variables for context managers.

        Currently possible variables are:

        * `browser_steal`
        """
        self.browser_steal = kwargs.get("browser_steal", self.browser_steal)
        return self

    def __enter__(self):
        """ This method will replace the current appliance in the store """
        stack.push(self)
        return self

    def _screenshot_capture_at_context_leave(self, exc_type, exc_val, exc_tb):

        try:
            from fixtures.artifactor_plugin import fire_art_hook
            from pytest import config
            from fixture.pytest_store import store
        except ImportError:
            logger.info('Not inside pytest run, ignoring')
            return

        if (
                exc_type is not None and not RUNNING_UNDER_SPROUT):
            from cfme.fixtures.pytest_selenium import take_screenshot
            logger.info("Before we pop this appliance, a screenshot and a traceback will be taken.")
            ss, ss_error = take_screenshot()
            full_tb = "".join(traceback.format_tb(exc_tb))
            short_tb = "{}: {}".format(exc_type.__name__, str(exc_val))
            full_tb = "{}\n{}".format(full_tb, short_tb)

            g_id = "appliance-cm-screenshot-{}".format(fauxfactory.gen_alpha(length=6))

            fire_art_hook(
                config, 'filedump',
                slaveid=store.slaveid,
                description="Appliance CM error traceback", contents=full_tb, file_type="traceback",
                display_type="danger", display_glyph="align-justify", group_id=g_id)

            if ss:
                fire_art_hook(
                    config, 'filedump',
                    slaveid=store.slaveid, description="Appliance CM error screenshot",
                    file_type="screenshot", mode="wb", contents_base64=True, contents=ss,
                    display_glyph="camera", group_id=g_id)
            if ss_error:
                fire_art_hook(
                    config, 'filedump',
                    slaveid=store.slaveid,
                    description="Appliance CM error screenshot failure", mode="w",
                    contents_base64=False, contents=ss_error, display_type="danger", group_id=g_id)
        elif exc_type is not None:
            logger.info("Error happened but we are not inside a test run so no screenshot now.")

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self._screenshot_capture_at_context_leave(exc_type, exc_val, exc_tb)
        except Exception:
            # repr is used in order to avoid having the appliance object in the log record
            logger.exception("taking a screenshot for %s failed", repr(self))
        finally:
            assert stack.pop() is self, 'appliance stack inconsistent'

    def __eq__(self, other):
        return isinstance(other, IPAppliance) and self.address == other.address

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.address)

    @cached_property
    def rest_logger(self):
        return create_sublogger('rest-api')

    # Configuration methods
    @logger_wrap("Configure IPAppliance: {}")
    def configure(self, log_callback=None, **kwargs):
        """Configures appliance - database setup, rename, ntp sync

        Utility method to make things easier.

        Note:
            db_address, name_to_set are not used currently.

        Args:
            db_address: Address of external database if set, internal database if ``None``
                        (default ``None``)
            name_to_set: Name to set the appliance name to if not ``None`` (default ``None``)
            region: Number to assign to region (default ``0``)
            fix_ntp_clock: Fixes appliance time if ``True`` (default ``True``)
            loosen_pgssl: Loosens postgres connections if ``True`` (default ``True``)
            key_address: Fetch encryption key from this address if set, generate a new key if
                         ``None`` (default ``None``)

        """

        log_callback("Configuring appliance {}".format(self.address))
        loosen_pgssl = kwargs.pop('loosen_pgssl', True)
        fix_ntp_clock = kwargs.pop('fix_ntp_clock', True)
        region = kwargs.pop('region', 0)
        key_address = kwargs.pop('key_address', None)
        with self as ipapp:
            ipapp.wait_for_ssh()
            self.deploy_merkyl(start=True, log_callback=log_callback)
            if fix_ntp_clock:
                self.fix_ntp_clock(log_callback=log_callback)
                # TODO: Handle external DB setup. Also in setup_database
            self.setup_database(
                region=region,
                key_address=key_address,
                log_callback=log_callback)
            self.wait_for_evm_service(timeout=1200, log_callback=log_callback)

            # Some conditionally ran items require the evm service be
            # restarted:
            restart_evm = False
            if loosen_pgssl:
                self.loosen_pgssl(log_callback=log_callback)
                restart_evm = True
            if self.version >= '5.8':
                self.configure_vm_console_cert(log_callback=log_callback)
                restart_evm = True
            if restart_evm:
                self.restart_evm_service(log_callback=log_callback)
            self.wait_for_web_ui(timeout=1800, log_callback=log_callback)

    # TODO: this method eventually needs to be moved to provider class..
    @logger_wrap("Configure GCE IPAppliance: {}")
    def configure_gce(self, log_callback=None):
        self.wait_for_ssh(timeout=1200)
        self.deploy_merkyl(start=True, log_callback=log_callback)
        # TODO: Fix NTP on GCE instances.
        # self.fix_ntp_clock(log_callback=log_callback)
        self.enable_internal_db(log_callback=log_callback)
        # evm serverd does not auto start on GCE instance..
        self.start_evm_service(log_callback=log_callback)
        self.wait_for_evm_service(timeout=1200, log_callback=log_callback)
        self.wait_for_web_ui(timeout=1800, log_callback=log_callback)
        self.loosen_pgssl(log_callback=log_callback)
        self.wait_for_web_ui(timeout=1800, log_callback=log_callback)

    @property
    def db_partition_extended(self):
        return self.ssh_client.run_command("ls /var/www/miq/vmdb/.db_partition_extended") == 0

    @logger_wrap("Extend DB partition")
    def extend_db_partition(self, log_callback=None):
        """Extends the /var partition with DB while shrinking the unused /repo partition"""
        if self.db_partition_extended:
            return
        with self.ssh_client as ssh:
            rc, out = ssh.run_command("df -h")
            log_callback("File systems before extending the DB partition:\n{}".format(out))
            ssh.run_command("umount /repo")
            ssh.run_command("lvreduce --force --size -9GB /dev/mapper/VG--CFME-lv_repo")
            ssh.run_command("mkfs.xfs -f /dev/mapper/VG--CFME-lv_repo")
            ssh.run_command("lvextend --resizefs --size +9GB /dev/mapper/VG--CFME-lv_var")
            ssh.run_command("mount -a")
            rc, out = ssh.run_command("df -h")
            log_callback("File systems after extending the DB partition:\n{}".format(out))
            ssh.run_command("touch /var/www/miq/vmdb/.db_partition_extended")

    @logger_wrap("Wait for no postgres connections")
    def drop_database(self, log_callback=None):
        """ Drops the vmdb_production database

            Note: EVM service has to be stopped for this to work.
        """
        def _db_dropped():
            rc, out = self.ssh_client.run_command(
                'systemctl restart {}-postgresql'.format(self.postgres_version), timeout=60)
            assert rc == 0, "Failed to restart postgres service: {}".format(out)
            self.ssh_client.run_command('dropdb vmdb_production', timeout=15)
            rc, out = self.ssh_client.run_command(
                "psql -l | grep vmdb_production | wc -l", timeout=15)
            return rc == 0
        wait_for(_db_dropped, delay=5, timeout=60, message="drop the vmdb_production DB")

    def seal_for_templatizing(self):
        """Prepares the VM to be "generalized" for saving as a template."""
        with self.ssh_client as ssh_client:
            # Seals the VM in order to work when spawned again.
            ssh_client.run_command("rm -rf /etc/ssh/ssh_host_*", ensure_host=True)
            if ssh_client.run_command(
                    "grep '^HOSTNAME' /etc/sysconfig/network", ensure_host=True).rc == 0:
                # Replace it
                ssh_client.run_command(
                    "sed -i -r -e 's/^HOSTNAME=.*$/HOSTNAME=localhost.localdomain/' "
                    "/etc/sysconfig/network", ensure_host=True)
            else:
                # Set it
                ssh_client.run_command(
                    "echo HOSTNAME=localhost.localdomain >> /etc/sysconfig/network",
                    ensure_host=True)
            ssh_client.run_command(
                "sed -i -r -e '/^HWADDR/d' /etc/sysconfig/network-scripts/ifcfg-eth0",
                ensure_host=True)
            ssh_client.run_command(
                "sed -i -r -e '/^UUID/d' /etc/sysconfig/network-scripts/ifcfg-eth0",
                ensure_host=True)
            ssh_client.run_command("rm -f /etc/udev/rules.d/70-*", ensure_host=True)
            # Fix SELinux things
            ssh_client.run_command("restorecon -R /etc/sysconfig/network-scripts", ensure_host=True)
            ssh_client.run_command("restorecon /etc/sysconfig/network", ensure_host=True)
            # Stop the evmserverd and move the logs somewhere
            ssh_client.run_command("systemctl stop evmserverd", ensure_host=True)
            ssh_client.run_command("mkdir -p /var/www/miq/vmdb/log/preconfigure-logs",
                ensure_host=True)
            ssh_client.run_command(
                "mv /var/www/miq/vmdb/log/*.log /var/www/miq/vmdb/log/preconfigure-logs/",
                ensure_host=True)
            ssh_client.run_command(
                "mv /var/www/miq/vmdb/log/*.gz /var/www/miq/vmdb/log/preconfigure-logs/",
                ensure_host=True)
            # Reduce swapping, because it can do nasty things to our providers
            ssh_client.run_command('echo "vm.swappiness = 1" >> /etc/sysctl.conf',
                ensure_host=True)

    def _encrypt_string(self, string):
        try:
            # Let's not log passwords
            logging.disable(logging.CRITICAL)
            rc, out = self.ssh_client.run_rails_command(
                "puts MiqPassword.encrypt('{}')".format(string))
            return out
        finally:
            logging.disable(logging.NOTSET)

    def _get_ems_ips(self, ems):
        ep_table = self.db["endpoints"]
        ip_addresses = set()
        for ep in self.db.session.query(ep_table).filter(ep_table.resource_id == ems.id):
            if ep.ipaddress is not None:
                ip_addresses.add(ep.ipaddress)
            elif ep.hostname is not None:
                if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ep.hostname) is not None:
                    ip_addresses.add(ep.hostname)
                else:
                    ip_address = resolve_hostname(ep.hostname)
                    if ip_address is not None:
                        ip_addresses.add(ip_address)
        return list(ip_addresses)

    def _list_ems(self):
        self.db.session.expire_all()
        ems_table = self.db["ext_management_systems"]
        # Fetch all providers at once, return empty list otherwise
        try:
            ems_list = list(self.db.session.query(ems_table))
        except Exception as ex:
            self.log.warning("Unable to query DB for managed providers: %s", str(ex))
            return []
        known_ems_list = []
        for ems in ems_list:
            # Skip any EMS types that we don't care about / can't recognize safely
            if not any(p_type in ems.type for p_type in RECOGNIZED_BY_IP + RECOGNIZED_BY_CREDS):
                continue
            known_ems_list.append(ems)
        return known_ems_list

    def _query_endpoints(self):
        if "endpoints" in self.db:
            return self._query_post_endpoints()
        else:
            return self._query_pre_endpoints()

    def _query_pre_endpoints(self):
        ems_table = self.db["ext_management_systems"]
        for ems in self.db.session.query(ems_table):
            yield ems.ipaddress, ems.hostname

    def _query_post_endpoints(self):
        """After Oct 5th, 2015, the ipaddresses and stuff was separated in a separate table."""
        ems_table = self.db["ext_management_systems"]
        ep = self.db["endpoints"]
        for ems in self.db.session.query(ems_table):
            for endpoint in self.db.session.query(ep).filter(ep.resource_id == ems.id):
                ipaddress = endpoint.ipaddress
                hostname = endpoint.hostname
                yield ipaddress, hostname

    @property
    def managed_known_providers(self):
        """Returns a set of provider crud objects of known providers managed by this appliance

        Note:
            Recognized by name only.
        """
        from utils.providers import list_providers
        prov_cruds = list_providers(use_global_filters=False, appliance=self)

        found_cruds = set()
        unrecognized_ems_names = set()
        for ems in self._list_ems():
            for prov in prov_cruds:
                # Name check is authoritative and the only proper way to recognize a known provider
                if ems.name == prov.name:
                    found_cruds.add(prov)
                    break
            else:
                unrecognized_ems_names.add(ems.name)
        if unrecognized_ems_names:
            self.log.warning(
                "Unrecognized managed providers: {}".format(', '.join(unrecognized_ems_names)))
        return list(found_cruds)

    @property
    def managed_provider_names(self):
        """Returns a list of names for all providers configured on the appliance

        Note:
            Unlike ``managed_known_providers``, this will also return names of providers that were
            not recognized, but are present.
        """
        return [ems.name for ems in self._list_ems()]

    def check_no_conflicting_providers(self):
        """ Checks that there are no conflicting providers set up on the appliance

        Raises:
            ApplianceException: If there are any conflicting providers present on the appliance

        Note:
            A conflicting provider is a provider with IP or credentials (depends on it's type)
            known to our automation but whose name doesn't match the one found in yamls.
            This is used to avoid hard-to-debug
         automation issues.
        """
        from utils.providers import list_providers

        def _recognized_by_ip(provider, ems):
            if any(p_type in ems.type for p_type in RECOGNIZED_BY_IP):
                try:
                    prov_ip = getattr(provider, 'ip_address', None) \
                        or resolve_hostname(provider.hostname)
                except AttributeError:
                    # Provider has no IP nor hostname; it should be looked up by credentials,
                    # not by IP address
                    return False
                if prov_ip in self._get_ems_ips(ems):
                    return True
            return False

        def _recognized_by_creds(provider, ems):
            if any(p_type in ems.type for p_type in RECOGNIZED_BY_CREDS):
                creds_table = self.db["authentications"]
                try:
                    default_creds = provider.credentials['default']
                except KeyError:
                    # Provider has no default credentials; it should be looked up by IP address,
                    # not by credentials
                    return False
                auth = self.db.session.query(creds_table)\
                    .filter(creds_table.resource_id == ems.id)\
                    .filter(creds_table.userid == default_creds.principal)\
                    .filter(creds_table.password == self._encrypt_string(default_creds.secret))\
                    .order_by(creds_table.id.desc())\
                    .first()
                if auth is not None:
                    return True
            return False

        prov_cruds = list_providers(use_global_filters=False)
        managed_known_providers_names = [prov.name for prov in self.managed_known_providers]
        for ems in self._list_ems():
            # EMS found among managed known providers can be ignored; they are not conflicting
            if ems.name in managed_known_providers_names:
                continue
            # Otherwise, if the EMS matches a provider in our yamls by IP or credentials
            # but isn't among managed providers -> same provider with a different name
            for prov in prov_cruds:
                if _recognized_by_ip(prov, ems) or _recognized_by_creds(prov, ems):
                    raise ApplianceException(
                        "Provider '{}' present on the appliance under the name '{}' instead"
                        " of expected '{}', please rename or remove it before continuing"
                        .format(prov.key, ems.name, prov.name))

    @property
    def has_os_infra(self):
        """If there is an OS Infra set up as a provider, some of the UI changes"""
        ems_table = self.db["ext_management_systems"]
        self.db.session.query(ems_table)
        count = self.db.session.query(ems_table).filter(
            ems_table.type == "EmsOpenstackInfra").count()
        return count > 0

    @property
    def has_non_os_infra(self):
        """If there is any non-OS-infra set up as a provider, some of the UI changes"""
        ems_table = self.db["ext_management_systems"]
        self.db.session.query(ems_table)
        count = self.db.session.query(ems_table).filter(
            ems_table.type != "EmsOpenstackInfra").count()
        return count > 0

    @classmethod
    def from_url(cls, url):
        return cls(urlparse(url))

    @cached_property
    def rest_api(self):
        return MiqApi(
            "{}://{}:{}/api".format(self.scheme, self.address, self.ui_port),
            (conf.credentials['default']['username'], conf.credentials['default']['password']),
            logger=self.rest_logger,
            verify_ssl=False)

    @cached_property
    def miqqe_version(self):
        """Returns version of applied JS patch or None if not present"""
        rc, out = self.ssh_client.run_command('grep "[0-9]\+" /var/www/miq/vmdb/.miqqe_version')
        if rc == 0:
            return int(out)
        return None

    @cached_property
    def address(self):
        # If address wasn't set in __init__, use the hostname from base_url
        if getattr(self, "_url", None) is not None:
            parsed_url = urlparse(self._url)
            return parsed_url.netloc
        else:
            parsed_url = urlparse(store.base_url)
            return parsed_url.netloc

    @cached_property
    def hostname(self):
        parsed_url = urlparse(self.url)
        return parsed_url.hostname

    @cached_property
    def product_name(self):
        try:
            # We need to print to a file here because the deprecation warnings make it hard
            # to get robust output and they do not seem to go to stderr
            result = self.ssh_client.run_rails_command(
                '"File.open(\'/tmp/product_name.txt\', \'w\') '
                '{|f| f.write(I18n.t(\'product.name\')) }"')
            result = self.ssh_client.run_command('cat /tmp/product_name.txt')
            return result.output
        except:
            logger.error(
                "Couldn't fetch the product name from appliance, using ManageIQ as default")
            return 'ManageIQ'

    @property
    def ui_port(self):
        parsed_url = urlparse(self.url)
        if parsed_url.port is not None:
            return parsed_url.port
        elif parsed_url.scheme == "https":
            return 443
        elif parsed_url.scheme == "http":
            return 80
        else:
            raise Exception("Unknown scheme {} for {}".format(parsed_url.scheme, store.base_url))

    @cached_property
    def scheme(self):
        return "https"  # By default

    @cached_property
    def url(self):
        return "{}://{}/".format(self.scheme, self.address)

    @cached_property
    def version(self):
        res = self.ssh_client.run_command('cat /var/www/miq/vmdb/VERSION')
        if res.rc != 0:
            raise RuntimeError('Unable to retrieve appliance VMDB version')
        return Version(res.output)

    @cached_property
    def build(self):
        if self.ssh_client.is_appliance_downstream():
            res = self.ssh_client.run_command('cat /var/www/miq/vmdb/BUILD')
            if res.rc != 0:
                raise RuntimeError('Unable to retrieve appliance VMDB version')
            return res.output.strip("\n")
        else:
            return "master"

    @cached_property
    def os_version(self):
        # Currently parses the os version out of redhat release file to allow for
        # rhel and centos appliances
        res = self.ssh_client.run_command(
            r"cat /etc/redhat-release | sed 's/.* release \(.*\) (.*/\1/' #)")
        if res.rc != 0:
            raise RuntimeError('Unable to retrieve appliance OS version')
        return Version(res.output)

    @cached_property
    def log(self):
        return create_sublogger(self.address)

    @cached_property
    def coverage(self):
        return ui_coverage.CoverageManager(self)

    def ssh_client_with_privatekey(self):
        with open(conf_path.join('appliance_private_key').strpath, 'w') as key:
            key.write(conf.credentials['ssh']['private_key'])
        connect_kwargs = {
            'hostname': self.hostname,
            'username': conf.credentials['ssh']['ssh-user'],
            'key_filename': conf_path.join('appliance_private_key').strpath,
        }
        ssh_client = ssh.SSHClient(**connect_kwargs)
        # FIXME: propperly store ssh clients we made
        store.ssh_clients_to_close.append(ssh_client)
        return ssh_client

    @cached_property
    def ssh_client(self):
        """Creates an ssh client connected to this appliance

        Returns: A configured :py:class:``utils.ssh.SSHClient`` instance.

        Usage:

            with appliance.ssh_client as ssh:
                status, output = ssh.run_command('...')

        Note:

            The credentials default to those found under ``ssh`` key in ``credentials.yaml``.

        """
        if not self.is_ssh_running:
            raise Exception('SSH is unavailable')

        # IPAppliance.ssh_client only connects to its address
        if self.openshift_creds:
            connect_kwargs = {
                'hostname': self.openshift_creds['hostname'],
                'username': self.openshift_creds['username'],
                'password': self.openshift_creds['password'],
                'container': self.container,
                'is_pod': True,
            }
            self.is_pod = True
        else:
            connect_kwargs = {
                'hostname': self.hostname,
                'username': conf.credentials['ssh']['username'],
                'password': conf.credentials['ssh']['password'],
                'container': self.container,
                'is_pod': False,
            }
        ssh_client = ssh.SSHClient(**connect_kwargs)
        try:
            ssh_client.get_transport().is_active()
            logger.info('default appliance ssh credentials are valid')
        except Exception as e:
            logger.error(e)
            logger.error('default appliance ssh credentials failed, trying establish ssh connection'
                         ' using ssh private key')
            ssh_client = self.ssh_client_with_privatekey()
        # FIXME: propperly store ssh clients we made
        store.ssh_clients_to_close.append(ssh_client)
        return ssh_client

    @property
    def db_ssh_client(self, **connect_kwargs):
        # Not lazycached to allow for the db address changing
        if self.is_db_internal:
            return self.ssh_client
        else:
            if self._db_ssh_client is None:
                self._db_ssh_client = self.ssh_client(hostname=self.db_address)
            return self._db_ssh_client

    @property
    def swap(self):
        """Retrieves the value of swap for the appliance. Might raise an exception if SSH fails.

        Return:
            An integer value of swap in the VM in megabytes. If ``None`` is returned, it means it
            was not possible to parse the command output.

        Raises:
            :py:class:`paramiko.ssh_exception.SSHException` or :py:class:`socket.error`
        """
        value = self.ssh_client.run_command(
            'free -m | tr -s " " " " | cut -f 3 -d " " | tail -n 1', reraise=True, timeout=15)
        try:
            value = int(value.output.strip())
        except (TypeError, ValueError):
            value = None
        return value

    def event_listener(self):
        """Returns an instance of the event listening class pointed to this appliance."""
        return EventListener(self)

    def diagnose_evm_failure(self):
        """Go through various EVM processes, trying to figure out what fails

        Returns: A string describing the error, or None if no errors occurred.

        This is intended to be run after an appliance is configured but failed for some reason,
        such as in the template tester.

        """
        logger.info('Diagnosing EVM failures, this can take a while...')

        if not self.address:
            return 'appliance has no IP Address; provisioning failed or networking is broken'

        logger.info('Checking appliance SSH Connection')
        if not self.is_ssh_running:
            return 'SSH is not running on the appliance'

        # Now for the DB
        logger.info('Checking appliance database')
        if not self.db_online:
            # postgres isn't running, try to start it
            cmd = 'systemctl restart {}-postgresql'.format(self.postgres_version)
            result = self.db_ssh_client.run_command(cmd)
            if result.rc != 0:
                return 'postgres failed to start:\n{}'.format(result.output)
            else:
                return 'postgres was not running for unknown reasons'

        if not self.db_has_database:
            return 'vmdb_production database does not exist'

        if not self.db_has_tables:
            return 'vmdb_production has no tables'

        # try to start EVM
        logger.info('Checking appliance evmserverd service')
        try:
            self.restart_evm_service()
        except ApplianceException as ex:
            return 'evmserverd failed to start:\n{}'.format(ex.args[0])

        # This should be pretty comprehensive, but we might add some net_checks for
        # 3000, 4000, and 80 at this point, and waiting a reasonable amount of time
        # before exploding if any of them don't appear in time after evm restarts.

    @logger_wrap("Fix NTP Clock: {}")
    def fix_ntp_clock(self, log_callback=None):
        """Fixes appliance time using ntpdate on appliance"""
        log_callback('Fixing appliance clock')
        client = self.ssh_client

        # checking whether chrony is installed
        check_cmd = 'yum list installed chrony'
        if client.run_command(check_cmd).rc != 0:
            raise ApplianceException("Chrony isn't installed")

        # # checking whether it is enabled and enable it
        is_enabled_cmd = 'systemctl is-enabled chronyd'
        if client.run_command(is_enabled_cmd).rc != 0:
            logger.debug("chrony will start on system startup")
            client.run_command('systemctl enable chronyd')
            client.run_command('systemctl daemon-reload')

        # # deploy config and start chrony if it isn't running
        server_template = 'server {srv} iburst'
        base_config = ['driftfile /var/lib/chrony/drift', 'makestep 10 10', 'rtcsync']
        try:
            logger.debug('obtaining clock servers from config file')
            clock_servers = conf.cfme_data.get('clock_servers', {})
            for clock_server in clock_servers:
                base_config.append(server_template.format(srv=clock_server))
        except IndexError:
            msg = 'No clock servers configured in cfme_data.yaml'
            log_callback(msg)
            raise ApplianceException(msg)

        filename = '/etc/chrony.conf'
        config_file = "\n".join(base_config)

        old_conf_file = client.run_command("cat {f}".format(f=filename)).output
        conf_file_updated = False
        if config_file != old_conf_file:
            logger.debug("chrony's config file isn't equal to prepared one, overwriting it")
            client.run_command('echo "{txt}" > {f}'.format(txt=config_file, f=filename))
            conf_file_updated = True

        if conf_file_updated or client.run_command('systemctl status chronyd').rc != 0:
            logger.debug('restarting chronyd')
            client.run_command('systemctl restart chronyd')

        # check that chrony is running correctly now
        result = client.run_command('chronyc tracking')
        if result.rc == 0:
            logger.info('chronyc is running correctly')
        else:
            raise ApplianceException("chrony doesn't work. "
                                     "Error message: {e}".format(e=result.output))

    @property
    def is_miqqe_patch_candidate(self):
        return self.version < "5.6.3"

    @property
    def miqqe_patch_applied(self):
        return self.miqqe_version == current_miqqe_version

    @logger_wrap("Patch appliance with MiqQE js: {}")
    def patch_with_miqqe(self, log_callback=None):

        # (local_path, remote_path, md5/None) trio
        autofocus_patch = pick({
            '5.5': 'autofocus.js.diff',
            '5.7': 'autofocus_57.js.diff'
        })
        patch_args = (
            (str(patches_path.join('miq_application.js.diff')),
             '/var/www/miq/vmdb/app/assets/javascripts/miq_application.js',
             None),
            (str(patches_path.join(autofocus_patch)),
             '/var/www/miq/vmdb/app/assets/javascripts/directives/autofocus.js',
             None),
        )

        for local_path, remote_path, md5 in patch_args:
            self.ssh_client.patch_file(local_path, remote_path, md5)

        self.precompile_assets()
        self.restart_evm_service()
        logger.info("Waiting for Web UI to start")
        wait_for(
            func=self.is_web_ui_running,
            message='appliance.is_web_ui_running',
            delay=20,
            timeout=300)
        logger.info("Web UI is up and running")
        self.ssh_client.run_command(
            "echo '{}' > /var/www/miq/vmdb/.miqqe_version".format(current_miqqe_version))
        # Invalidate cached version
        del self.miqqe_version

    @logger_wrap("Work around missing Gem file: {}")
    def workaround_missing_gemfile(self, log_callback=None):
        """Fix Gemfile issue.

        Early 5.4 builds have issues with Gemfile not present (BUG 1191496). This circumvents the
        issue with pointing the env variable that Bundler uses to get the Gemfile to the Gemfile in
        vmdb which *should* be correct.

        When this issue is resolved, this method will do nothing.
        """
        client = self.ssh_client
        status, out = client.run_command("ls /opt/rh/cfme-gemset")
        if status != 0:
            return  # Not needed
        log_callback('Fixing Gemfile issue')
        # Check if the error is there
        status, out = client.run_rails_command("puts 1")
        if status == 0:
            return  # All OK!
        client.run_command('echo "export BUNDLE_GEMFILE=/var/www/miq/vmdb/Gemfile" >> /etc/bashrc')
        # To be 100% sure
        self.reboot(wait_for_web_ui=False, log_callback=log_callback)

    @logger_wrap("Precompile assets: {}")
    def precompile_assets(self, log_callback=None):
        """Precompile the static assets (images, css, etc) on an appliance

        """
        log_callback('Precompiling assets')
        client = self.ssh_client

        store.terminalreporter.write_line('Precompiling assets')
        store.terminalreporter.write_line(
            'THIS IS NOT STUCK. Just wait until it\'s done, it will be only done once', red=True)
        store.terminalreporter.write_line('Phase 1 of 2: rake assets:clobber')
        status, out = client.run_rake_command("assets:clobber")
        if status != 0:
            msg = 'Appliance {} failed to nuke old assets'.format(self.address)
            log_callback(msg)
            raise ApplianceException(msg)

        store.terminalreporter.write_line('Phase 2 of 2: rake assets:precompile')
        status, out = client.run_rake_command("assets:precompile")
        if status != 0:
            msg = 'Appliance {} failed to precompile assets'.format(self.address)
            log_callback(msg)
            raise ApplianceException(msg)

        store.terminalreporter.write_line('Asset precompilation done')
        return status

    @logger_wrap("Backup database: {}")
    def backup_database(self, database_path="/tmp/evm_db.backup", log_callback=None):
        """Backup VMDB database

        """
        log_callback('Backing up database')
        status, output = self.ssh_client.run_rake_command(
            'evm:db:backup:local --trace -- --local-file "{}" --dbname vmdb_production'.format(
                database_path))
        if status != 0:
            msg = 'Failed to backup database'
            log_callback(msg)
            raise ApplianceException(msg)

    @logger_wrap("Restore database: {}")
    def restore_database(self, database_path="/tmp/evm_db.backup", log_callback=None):
        """Restore VMDB database

        """
        log_callback('Restoring database')
        status, output = self.ssh_client.run_rake_command(
            'evm:db:restore:local --trace -- --local-file "{}"'.format(database_path))
        if status != 0:
            msg = 'Failed to restore database on appl {}, output is {}'.format(self.address,
                output)
            log_callback(msg)
            raise ApplianceException(msg)

    @logger_wrap("Database setup: {}")
    def setup_database(self, log_callback=None, **kwargs):
        """Configure database

        On downstream appliances, invokes the internal database setup.
        On all appliances waits for database to be ready.

        """
        log_callback('Starting DB setup')
        if self.version != LATEST:
            # We only execute this on downstream appliances.
            # TODO: Handle external DB setup. Probably pop the db_address and decide on that one.
            self.enable_internal_db(log_callback=log_callback, **kwargs)

        # Make sure the database is ready
        wait_for(func=lambda: self.is_db_ready,
            message='appliance db ready', delay=20, num_sec=1200)

        log_callback('DB setup complete')

    @logger_wrap("Clone automate domain: {}")
    def clone_domain(self, source="ManageIQ", dest="Default", log_callback=None):
        """Clones Automate domain

        Args:
            src: Source domain name.
            dst: Destination domain name.

        """
        client = self.ssh_client

        # Make sure the database is ready
        log_callback('Waiting for database')
        self.wait_for_db()

        # Make sure the working dir exists
        client.run_command('mkdir -p /tmp/{}'.format(source))

        export_opts = 'DOMAIN={} EXPORT_DIR=/tmp/{} PREVIEW=false OVERWRITE=true'.format(source,
            source)
        export_cmd = 'evm:automate:export {}'.format(export_opts)
        log_callback('Exporting domain ({}) ...'.format(export_cmd))
        status, output = client.run_rake_command(export_cmd)
        if status != 0:
            msg = 'Failed to export {} domain'.format(source)
            log_callback(msg)
            raise ApplianceException(msg)

        ro_fix_cmd = ("sed -i 's/system: true/system: false/g' "
                      "/tmp/{}/{}/__domain__.yaml".format(source, source))
        status, output = client.run_command(ro_fix_cmd)
        if status != 0:
            msg = 'Setting {} domain to read/write failed'.format(dest)
            log_callback(msg)
            raise ApplianceException(msg)

        import_opts = 'DOMAIN={} IMPORT_DIR=/tmp/{} PREVIEW=false'.format(source, source)
        import_opts += ' OVERWRITE=true IMPORT_AS={} ENABLED=true'.format(dest)
        import_cmd = 'evm:automate:import {}'.format(import_opts)
        log_callback('Importing domain ({}) ...'.format(import_cmd))
        status, output = client.run_rake_command(import_cmd)
        if status != 0:
            msg = 'Failed to import {} domain'.format(dest)
            log_callback(msg)
            raise ApplianceException(msg)

        return status, output

    @logger_wrap("Deploying Merkyl: {}")
    def deploy_merkyl(self, start=False, log_callback=None):
        """Deploys the Merkyl log relay service to the appliance"""

        client = self.ssh_client

        client.run_command('mkdir -p /root/merkyl')
        for filename in ['__init__.py', 'merkyl.tpl', ('bottle.py.dontflake', 'bottle.py'),
                         'allowed.files']:
            try:
                src, dest = filename
            except (TypeError, ValueError):
                # object is not iterable or too many values to unpack
                src = dest = filename
            log_callback('Sending {} to appliance'.format(src))
            client.put_file(data_path.join(
                'bundles', 'merkyl', src).strpath, os.path.join('/root/merkyl', dest))

        client.put_file(data_path.join(
            'bundles', 'merkyl', 'merkyl').strpath, os.path.join('/etc/init.d/merkyl'))
        client.run_command('chmod 775 /etc/init.d/merkyl')
        client.run_command(
            '/bin/bash -c \'if ! [[ $(iptables -L -n | grep "state NEW tcp dpt:8192") ]]; then '
            'iptables -I INPUT 6 -m state --state NEW -m tcp -p tcp --dport 8192 -j ACCEPT; fi\'')

        if start:
            log_callback("Starting ...")
            client.run_command('systemctl restart merkyl')
            log_callback("Setting it to start after reboot")
            client.run_command("chkconfig merkyl on")

    def get_repofile_list(self):
        """Returns list of repofiles present at the appliance.

        Ignores certain files, like redhat.repo.
        """
        repofiles = self.ssh_client.run_command('ls /etc/yum.repos.d').output.strip().split('\n')
        return [f for f in repofiles if f not in {"redhat.repo"} and f.endswith(".repo")]

    def read_repos(self):
        """Reads repofiles so it gives you mapping of id and url."""
        result = {}
        name_regexp = re.compile(r"^\[update-([^\]]+)\]")
        baseurl_regexp = re.compile(r"baseurl\s*=\s*([^\s]+)")
        for repofile in self.get_repofile_list():
            rc, out = self.ssh_client.run_command("cat /etc/yum.repos.d/{}".format(repofile))
            if rc != 0:
                # Something happened meanwhile?
                continue
            out = out.strip()
            name_match = name_regexp.search(out)
            if name_match is None:
                continue
            baseurl_match = baseurl_regexp.search(out)
            if baseurl_match is None:
                continue
            result[name_match.groups()[0]] = baseurl_match.groups()[0]
        return result

    # Regexp that looks for product type and version in the update URL
    product_url_regexp = re.compile(
        r"/((?:[A-Z]+|CloudForms|rhel|RHEL_Guest))(?:-|/|/server/)(\d+[^/]*)/")

    def find_product_repos(self):
        """Returns a dictionary of products, where the keys are names of product (repos) and values
            are dictionaries where keys are the versions and values the names of the repositories.
        """
        products = {}
        for repo_name, repo_url in self.read_repos().iteritems():
            match = self.product_url_regexp.search(repo_url)
            if match is None:
                continue
            product, ver = match.groups()
            if product not in products:
                products[product] = {}
            products[product][ver] = repo_name
        return products

    def write_repofile(self, repo_id, repo_url, **kwargs):
        """Wrapper around writing a repofile. You can specify conf options in kwargs."""
        if "gpgcheck" not in kwargs:
            kwargs["gpgcheck"] = 0
        if "enabled" not in kwargs:
            kwargs["enabled"] = 1
        filename = "/etc/yum.repos.d/{}.repo".format(repo_id)
        logger.info("Writing a new repofile %s %s", repo_id, repo_url)
        self.ssh_client.run_command('echo "[update-{}]" > {}'.format(repo_id, filename))
        self.ssh_client.run_command('echo "name=update-url-{}" >> {}'.format(repo_id, filename))
        self.ssh_client.run_command('echo "baseurl={}" >> {}'.format(repo_url, filename))
        for k, v in kwargs.iteritems():
            self.ssh_client.run_command('echo "{}={}" >> {}'.format(k, v, filename))
        return repo_id

    def add_product_repo(self, repo_url, **kwargs):
        """This method ensures that when we add a new repo URL, there will be no other version
            of such product present in the yum.repos.d. You can specify conf options in kwargs. They
            will be applied only to newly created repo file.

        Returns:
            The repo id.
        """
        match = self.product_url_regexp.search(repo_url)
        if match is None:
            raise ValueError(
                "The URL {} does not contain information about product and version.".format(
                    repo_url))
        for repo_id, url in self.read_repos().iteritems():
            if url == repo_url:
                # It is already there, so just enable it
                self.enable_disable_repo(repo_id, True)
                return repo_id
        product, ver = match.groups()
        repos = self.find_product_repos()
        if product in repos:
            for v, i in repos[product].iteritems():
                logger.info("Deleting %s repo with version %s (%s)", product, v, i)
                self.ssh_client.run_command("rm -f /etc/yum.repos.d/{}.repo".format(i))
        return self.write_repofile(fauxfactory.gen_alpha(), repo_url, **kwargs)

    def enable_disable_repo(self, repo_id, enable):
        logger.info("%s repository %s", "Enabling" if enable else "Disabling", repo_id)
        return self.ssh_client.run_command(
            "sed -i 's/^enabled=./enabled={}/' /etc/yum.repos.d/{}.repo".format(
                1 if enable else 0, repo_id)).rc == 0

    @logger_wrap("Update RHEL: {}")
    def update_rhel(self, *urls, **kwargs):
        """Update RHEL on appliance

        Will pull URLs from the 'updates_urls' environment variable (whitespace-separated URLs),
        or cfme_data.

        If the env var is not set, URLs will be pulled from cfme_data.
        If the env var is set, it is the only source for update URLs.

        Generic rhel update URLs cfme_data.get('basic_info', {})['rhel_updates_urls'] (yaml list)
        On downstream builds, an additional RH SCL updates url can be inserted at
        cfme_data.get('basic_info', {})['rhscl_updates_urls'].

        If the ``skip_broken`` kwarg is passed, and evaluated as True, broken packages will be
        ignored in the yum update.


        """
        urls = list(urls)
        log_callback = kwargs.pop("log_callback")
        skip_broken = kwargs.pop("skip_broken", False)
        reboot = kwargs.pop("reboot", True)
        streaming = kwargs.pop("streaming", False)
        cleanup = kwargs.pop('cleanup', False)
        log_callback('updating appliance')
        if not urls:
            basic_info = conf.cfme_data.get('basic_info', {})
            if os.environ.get('updates_urls'):
                # try to pull URLs from env if var is non-empty
                urls.extend(os.environ['update_urls'].split())
            else:
                # fall back to cfme_data
                if self.version >= "5.5":
                    updates_url = basic_info.get('rhel7_updates_url')
                else:
                    updates_url = basic_info.get('rhel_updates_url')

                if updates_url:
                    urls.append(updates_url)

        if streaming:
            client = self.ssh_client(stream_output=True)
        else:
            client = self.ssh_client

        if cleanup:
            client.run_command(
                "cd /etc/yum.repos.d && find . -not -name 'redhat.repo' "
                "-not -name 'rhel-source.repo' -not -name . -exec rm {} \;")

        for url in urls:
            self.add_product_repo(url)

        # update
        log_callback('Running rhel updates on appliance')
        # clean yum beforehand to clear metadata from earlier update repos, if any
        try:
            skip = '--skip-broken' if skip_broken else ''
            result = client.run_command('yum update -y --nogpgcheck {}'.format(skip),
                timeout=3600)
        except socket.timeout:
            msg = 'SSH timed out while updating appliance, exiting'
            log_callback(msg)
            # failure to update is fatal, kill this process
            raise KeyboardInterrupt(msg)

        self.log.error(result.output)
        if result.rc != 0:
            self.log.error('appliance update failed')
            msg = 'Appliance {} failed to update RHEL, error in logs'.format(self.address)
            log_callback(msg)
            raise ApplianceException(msg)

        if reboot:
            self.reboot(wait_for_web_ui=False, log_callback=log_callback)

        return result

    def utc_time(self):
        client = self.ssh_client
        status, output = client.run_command('date --iso-8601=seconds -u')
        if not status:
            return dateutil.parser.parse(output)
        else:
            raise Exception("Couldn't get datetime: {}".format(output))

    @logger_wrap("Loosen pgssl: {}")
    def loosen_pgssl(self, with_ssl=False, log_callback=None):
        """Loosens postgres connections"""

        log_callback('Loosening postgres permissions')

        # Init SSH client
        client = self.ssh_client

        # set root password
        cmd = "psql -d vmdb_production -c \"alter user {} with password '{}'\"".format(
            conf.credentials['database']['username'], conf.credentials['database']['password']
        )
        client.run_command(cmd)

        # back up pg_hba.conf
        scl = self.postgres_version
        client.run_command('mv /opt/rh/{scl}/root/var/lib/pgsql/data/pg_hba.conf '
                           '/opt/rh/{scl}/root/var/lib/pgsql/data/pg_hba.conf.sav'.format(scl=scl))

        if with_ssl:
            ssl = 'hostssl all all all cert map=sslmap'
        else:
            ssl = ''

        # rewrite pg_hba.conf
        write_pg_hba = dedent("""\
        cat > /opt/rh/{scl}/root/var/lib/pgsql/data/pg_hba.conf <<EOF
        local all postgres,root trust
        host all all 0.0.0.0/0 md5
        {ssl}
        EOF
        """.format(ssl=ssl, scl=scl))
        client.run_command(write_pg_hba)
        client.run_command("chown postgres:postgres "
            "/opt/rh/{scl}/root/var/lib/pgsql/data/pg_hba.conf".format(scl=scl))

        # restart postgres
        status, out = client.run_command("systemctl restart {scl}-postgresql".format(scl=scl))
        return status

    @logger_wrap("Enable internal DB: {}")
    def enable_internal_db(self, region=0, key_address=None, db_password=None,
                           ssh_password=None, log_callback=None):
        """Enables internal database

        Args:
            region: Region number of the CFME appliance.
            key_address: Address of CFME appliance where key can be fetched.

        Note:
            If key_address is None, a new encryption key is generated for the appliance.
        """
        log_callback('Enabling internal DB (region {}) on {}.'.format(region, self.address))
        self.db_address = self.address
        clear_property_cache(self, 'db')

        client = self.ssh_client

        # Defaults
        db_password = db_password or conf.credentials['database']['password']
        ssh_password = ssh_password or conf.credentials['ssh']['password']

        if self.has_cli:
            # use the cli
            if key_address:
                status, out = client.run_command(
                    'appliance_console_cli --region {} --internal --fetch-key {} -p {} -a {}'
                    .format(region, key_address, db_password, ssh_password)
                )
            else:
                status, out = client.run_command(
                    'appliance_console_cli --region {} --internal --force-key -p {}'
                    .format(region, db_password)
                )
        else:
            # no cli, use the enable internal db script
            rbt_repl = {
                'miq_lib': '/var/www/miq/lib',
                'region': region,
                'postgres_version': self.postgres_version
            }

            # Find and load our rb template with replacements
            rbt = datafile.data_path_for_filename('enable-internal-db.rbt', scripts_path.strpath)
            rb = datafile.load_data_file(rbt, rbt_repl)

            # sent rb file over to /tmp
            remote_file = '/tmp/{}'.format(fauxfactory.gen_alphanumeric())
            client.put_file(rb.name, remote_file)

            # Run the rb script, clean it up when done
            status, out = client.run_command('ruby {}'.format(remote_file))
            client.run_command('rm {}'.format(remote_file))

        return status, out

    @logger_wrap("Enable external DB: {}")
    def enable_external_db(self, db_address, region=0, db_name=None,
            db_username=None, db_password=None, log_callback=None):
        """Enables external database

        Args:
            db_address: Address of the external database
            region: Number of region to join
            db_name: Name of the external DB
            db_username: Username to access the external DB
            db_password: Password to access the external DB

        Returns a tuple of (exitstatus, script_output) for reporting, if desired
        """
        log_callback('Enabling external DB (db_address {}, region {}) on {}.'
            .format(db_address, region, self.address))
        # reset the db address and clear the cached db object if we have one
        self.db_address = db_address
        clear_property_cache(self, 'db')

        # default
        db_name = db_name or 'vmdb_production'
        db_username = db_username or conf.credentials['database']['username']
        db_password = db_password or conf.credentials['database']['password']

        client = self.ssh_client

        if self.has_cli:
            # copy v2 key
            master_client = client(hostname=self.db_address)
            rand_filename = "/tmp/v2_key_{}".format(fauxfactory.gen_alphanumeric())
            master_client.get_file("/var/www/miq/vmdb/certs/v2_key", rand_filename)
            client.put_file(rand_filename, "/var/www/miq/vmdb/certs/v2_key")

            # enable external DB with cli
            status, out = client.run_command(
                'appliance_console_cli '
                '--hostname {} --region {} --dbname {} --username {} --password {}'.format(
                    self.db_address, region, db_name, db_username, db_password
                )
            )
        else:
            # no cli, use the enable external db script
            rbt_repl = {
                'miq_lib': '/var/www/miq/lib',
                'host': self.db_address,
                'region': region,
                'database': db_name,
                'username': db_username,
                'password': db_password
            }

            # Find and load our rb template with replacements
            rbt = datafile.data_path_for_filename('enable-internal-db.rbt', scripts_path.strpath)
            rb = datafile.load_data_file(rbt, rbt_repl)

            # Init SSH client and sent rb file over to /tmp
            remote_file = '/tmp/{}'.format(fauxfactory.gen_alphanumeric())
            client.put_file(rb.name, remote_file)

            # Run the rb script, clean it up when done
            status, out = client.run_command('ruby {}'.format(remote_file))
            client.run_command('rm {}'.format(remote_file))

        if status != 0:
            self.log.error('error enabling external db')
            self.log.error(out)
            msg = ('Appliance {} failed to enable external DB running on {}'
                  .format(self.address, db_address))
            log_callback(msg)
            raise ApplianceException(msg)

        return status, out

    def is_dedicated_db_active(self):
        return_code, output = self.ssh_client.run_command(
            "systemctl status {}-postgresql.service | grep running".format(
                self.postgres_version))
        return return_code == 0

    def _check_appliance_ui_wait_fn(self):
        # Get the URL, don't verify ssl cert
        try:
            response = requests.get(self.url, timeout=15, verify=False)
            if response.status_code == 200:
                self.log.info("Appliance online")
                return True
            else:
                self.log.debug('Appliance online, status code %s', response.status_code)
        except requests.exceptions.Timeout:
            self.log.debug('Appliance offline, connection timed out')
        except ValueError:
            # requests exposes invalid URLs as ValueErrors, which is excellent
            raise
        except Exception as ex:
            self.log.debug('Appliance online, but connection failed: %s', str(ex))
        return False

    def is_web_ui_running(self, unsure=False):
        """Triple checks if web UI is up and running

        Args:
            unsure: Variable to return when not sure if web UI is running or not
                    (default ``False``)

        """
        num_of_tries = 3
        was_running_count = 0
        for try_num in range(num_of_tries):
            if self._check_appliance_ui_wait_fn():
                was_running_count += 1
            sleep(3)

        if was_running_count == 0:
            return False
        elif was_running_count == num_of_tries:
            return True
        else:
            return unsure

    def _evm_service_command(self, command, log_callback, expected_exit_code=None):
        """Runs given systemctl command against the ``evmserverd`` service
        Args:
            command: Command to run, e.g. "start"
            expected_exit_code: If the exit codes don't match, ApplianceException is raised
        """
        log_callback("Running command '{}' against the evmserverd service".format(command))
        with self.ssh_client as ssh:
            status, output = ssh.run_command('systemctl {} evmserverd'.format(command))

        if expected_exit_code is not None and status != expected_exit_code:
            msg = 'Failed to {} evmserverd on {}\nError: {}'.format(command, self.address, output)
            log_callback(msg)
            raise ApplianceException(msg)

        return status

    @logger_wrap("Status of EVM service: {}")
    def is_evm_service_running(self, log_callback=None):
        """Checks the ``evmserverd`` service status on this appliance
        """
        return self._evm_service_command("status", log_callback=log_callback) == 0

    @logger_wrap("Stop EVM Service: {}")
    def stop_evm_service(self, log_callback=None):
        """Stops the ``evmserverd`` service on this appliance
        """
        self._evm_service_command('stop', expected_exit_code=0, log_callback=log_callback)

    @logger_wrap("Start EVM Service: {}")
    def start_evm_service(self, log_callback=None):
        """Starts the ``evmserverd`` service on this appliance
        """
        self._evm_service_command('start', expected_exit_code=0, log_callback=log_callback)

    @logger_wrap("Restart EVM Service: {}")
    def restart_evm_service(self, rude=False, log_callback=None):
        """Restarts the ``evmserverd`` service on this appliance
        """
        store.terminalreporter.write_line('evmserverd is being restarted, be patient please')
        with self.ssh_client as ssh:
            if rude:
                log_callback('restarting evm service by killing processes')
                status, msg = ssh.run_command(
                    'killall -9 ruby; systemctl restart {}-postgresql'.format(
                        self.postgres_version))
                self._evm_service_command("start", expected_exit_code=0, log_callback=log_callback)
            else:
                self._evm_service_command(
                    "restart", expected_exit_code=0, log_callback=log_callback)
        self.server_details_changed()

    @logger_wrap("Waiting for EVM service: {}")
    def wait_for_evm_service(self, timeout=900, log_callback=None):
        """Waits for the evemserverd service to be running

        Args:
            timeout: Number of seconds to wait until timeout (default ``900``)
        """
        log_callback('Waiting for evmserverd to be running')
        result, wait = wait_for(self.is_evm_service_running, num_sec=timeout,
                                fail_condition=False, delay=10)
        return result

    @logger_wrap("Rebooting Appliance: {}")
    def reboot(self, wait_for_web_ui=True, log_callback=None):
        log_callback('Rebooting appliance')
        client = self.ssh_client

        old_uptime = client.uptime()
        status, out = client.run_command('reboot')

        wait_for(lambda: client.uptime() < old_uptime, handle_exception=True,
            num_sec=600, message='appliance to reboot', delay=10)

        if wait_for_web_ui:
            self.wait_for_web_ui()

    @logger_wrap("Waiting for web_ui: {}")
    def wait_for_web_ui(self, timeout=900, running=True, log_callback=None):
        """Waits for the web UI to be running / to not be running

        Args:
            timeout: Number of seconds to wait until timeout (default ``600``)
            running: Specifies if we wait for web UI to start or stop (default ``True``)
                     ``True`` == start, ``False`` == stop
        """
        prefix = "" if running else "dis"
        (log_callback or self.log.info)('Waiting for web UI to ' + prefix + 'appear')
        result, wait = wait_for(self._check_appliance_ui_wait_fn, num_sec=timeout,
            fail_condition=not running, delay=10)
        return result

    @logger_wrap("Install VDDK: {}")
    def install_vddk(self, reboot=True, force=False, vddk_url=None, log_callback=None,
                     wait_for_web_ui_after_reboot=False):
        """Install the vddk on a appliance"""

        def log_raise(exception_class, message):
            log_callback(message)
            raise exception_class(message)

        if vddk_url is None:  # fallback to VDDK 5.5
            vddk_url = conf.cfme_data.get("basic_info", {}).get("vddk_url", None).get("v5_5", None)
        if vddk_url is None:
            raise Exception("vddk_url not specified!")

        with self.ssh_client as client:
            is_already_installed = False
            if client.run_command('test -d /usr/lib/vmware-vix-disklib/lib64')[0] == 0:
                is_already_installed = True

            if not is_already_installed or force:

                # start
                filename = vddk_url.split('/')[-1]

                # download
                log_callback('Downloading VDDK')
                result = client.run_command('curl {} -o {}'.format(vddk_url, filename))
                if result.rc != 0:
                    log_raise(Exception, "Could not download VDDK")

                # install
                log_callback('Installing vddk')
                status, out = client.run_command(
                    'yum -y install {}'.format(filename))
                if status != 0:
                    log_raise(
                        Exception, 'VDDK installation failure (rc: {})\n{}'.format(out, status))

                # verify
                log_callback('Verifying vddk')
                status, out = client.run_command('ldconfig -p | grep vix')
                if len(out) < 2:
                    log_raise(
                        Exception,
                        "Potential installation issue, libraries not detected\n{}".format(out))

                # reboot
                if reboot:
                    self.reboot(log_callback=log_callback,
                                wait_for_web_ui=wait_for_web_ui_after_reboot)
                else:
                    log_callback('A reboot is required before vddk will work')

    @logger_wrap("Install Netapp SDK: {}")
    def install_netapp_sdk(self, sdk_url=None, reboot=False, log_callback=None):
        """Installs the Netapp SDK.

        Args:
            sdk_url: Where the SDK zip file is located? (optional)
            reboot: Whether to reboot the appliance afterwards? (Default False but reboot is needed)
        """

        def log_raise(exception_class, message):
            log_callback(message)
            raise exception_class(message)

        if sdk_url is None:
            try:
                sdk_url = conf.cfme_data['basic_info']['netapp_sdk_url']
            except KeyError:
                raise Exception("cfme_data.yaml/basic_info/netapp_sdk_url is not present!")

        filename = sdk_url.split('/')[-1]
        foldername = os.path.splitext(filename)[0]

        with self.ssh_client as ssh:
            log_callback('Downloading SDK from {}'.format(sdk_url))
            status, out = ssh.run_command(
                'wget {url} -O {file} > /root/unzip.out 2>&1'.format(
                    url=sdk_url, file=filename))
            if status != 0:
                log_raise(Exception, 'Could not download Netapp SDK: {}'.format(out))

            log_callback('Extracting SDK ({})'.format(filename))
            status, out = ssh.run_command(
                'unzip -o -d /var/www/miq/vmdb/lib/ {}'.format(filename))
            if status != 0:
                log_raise(Exception, 'Could not extract Netapp SDK: {}'.format(out))

            path = '/var/www/miq/vmdb/lib/{}/lib/linux-64'.format(foldername)
            # Check if we haven't already added this line
            if ssh.run_command("grep -F '{}' /etc/default/evm".format(path)).rc != 0:
                log_callback('Installing SDK ({})'.format(foldername))
                status, out = ssh.run_command(
                    'echo "export LD_LIBRARY_PATH=\$LD_LIBRARY_PATH:{}" >> /etc/default/evm'.format(
                        path))
                if status != 0:
                    log_raise(Exception, 'SDK installation failure ($?={}): {}'.format(status, out))
            else:
                log_callback("Not needed to install, already done")

            log_callback('ldconfig')
            ssh.run_command('ldconfig')

            log_callback('Modifying YAML configuration')
            c_yaml = self.get_yaml_config()
            c_yaml['product']['storage'] = True
            self.set_yaml_config(c_yaml)

            # To mark that we installed netapp
            ssh.run_command("touch /var/www/miq/vmdb/HAS_NETAPP")

            if reboot:
                self.reboot(log_callback=log_callback)
            else:
                log_callback(
                    'Appliance must be restarted before the netapp functionality can be used.')
        clear_property_cache(self, 'is_storage_enabled')

    def wait_for_db(self, timeout=600):
        """Waits for appliance database to be ready

        Args:
            timeout: Number of seconds to wait until timeout (default ``180``)
        """
        wait_for(func=lambda: self.is_db_ready,
                 message='appliance.is_db_ready',
                 delay=20,
                 num_sec=timeout)

    def wait_for_ssh(self, timeout=600):
        """Waits for appliance SSH connection to be ready

        Args:
            timeout: Number of seconds to wait until timeout (default ``600``)
        """
        wait_for(func=lambda: self.is_ssh_running,
                 message='appliance.is_ssh_running',
                 delay=5,
                 num_sec=timeout)

    @property
    def is_supervisord_running(self):
        output = self.ssh_client.run_command("systemctl status supervisord")
        return output.success

    @property
    def is_embedded_ensible_role_enabled(self):
        return self.server_roles.get("embedded_ansible", False)

    @property
    def is_embedded_ansible_running(self):
        return self.is_embedded_ensible_role_enabled and self.is_supervisord_running

    def wait_for_embedded_ansible(self, timeout=900):
        """Waits for embedded ansible to be ready

        Args:
            timeout: Number of seconds to wait until timeout (default ``900``)
        """
        wait_for(
            func=lambda: self.is_embedded_ansible_running,
            message='appliance.is_embedded_ansible_running',
            delay=60,
            num_sec=timeout
        )

    @cached_property
    def get_host_address(self):
        try:
            server = self.get_yaml_config().get('server', None)
            if server:
                return server.get('host', None)
        except Exception as e:
            logger.exception(e)
            self.log.error('Exception occured while fetching host address')

    def wait_for_host_address(self):
        try:
            wait_for(func=lambda: getattr(self, 'get_host_address'),
                     fail_condition=None,
                     delay=5,
                     num_sec=120)
            return self.get_host_address
        except Exception as e:
            logger.exception(e)
            self.log.error('waiting for host address from yaml_config timedout')

    @cached_property
    def db_address(self):
        # pulls the db address from the appliance by default, falling back to the appliance
        # ip address (and issuing a warning) if that fails. methods that set up the internal
        # db should set db_address to something else when they do that
        if self.db_host:
            return self.db_host
        try:
            db = self.wait_for_host_address()
            if db is None:
                return self.address
            db = db.strip()
            ip_addr = self.ssh_client.run_command('ip address show')
            if db in ip_addr.output or db.startswith('127') or 'localhost' in db:
                # address is local, use the appliance address
                return self.address
            else:
                return db
        except (IOError, KeyError) as exc:
            self.log.error('Unable to pull database address from appliance')
            self.log.exception(exc)
            return self.address

    @cached_property
    def db(self):
        # slightly crappy: anything that changes self.db_address should also del(self.db)
        return db.Db(self.db_address)

    @property
    def is_db_enabled(self):
        if self.db_address is None:
            return False
        return True

    @property
    def is_db_internal(self):
        if self.db_address == self.address:
            return True
        return False

    @property
    def is_db_ready(self):
        # Using 'and' chain instead of all(...) to
        # prevent calling more things after a step fails
        return self.db_online and self.db_has_database and self.db_has_tables

    @property
    def db_online(self):
        db_check_command = ('psql -U postgres -t  -c "select now()" postgres')
        result = self.db_ssh_client.run_command(db_check_command)
        return result.rc == 0

    @property
    def db_has_database(self):
        db_check_command = ('psql -U postgres -t  -c "SELECT datname FROM pg_database '
            'WHERE datname LIKE \'vmdb_%\';" postgres | grep -q vmdb_production')
        result = self.db_ssh_client.run_command(db_check_command)
        return result.rc == 0

    @property
    def db_has_tables(self):
        db_check_command = ('psql -U postgres -t  -c "SELECT * FROM information_schema.tables '
            'WHERE table_schema = \'public\';" vmdb_production | grep -q vmdb_production')
        result = self.db_ssh_client.run_command(db_check_command)
        return result.rc == 0

    @property
    def is_ssh_running(self):
        return net_check(ports.SSH, self.hostname, force=True)

    @property
    def has_cli(self):
        if self.ssh_client.run_command('ls -l /bin/appliance_console_cli')[0] == 0:
            return True
        else:
            return False

    @property
    def is_idle(self):
        """Return appliance idle state measured by last production.log activity.
        It runs one liner script, which first gathers current date on appliance and then gathers
        date of last entry in production.log(which has to be parsed) with /api calls filtered
        (These calls occur every minute.)
        Then it deducts that last time in log from current date and if it is lower than idle_time it
        returns False else True.

        Args:

        Returns:
            True if appliance is idling for longer or equal to idle_time seconds.
            False if appliance is not idling for longer or equal to idle_time seconds.
        """
        idle_time = 3600
        ssh_output = self.ssh_client.run_command('if [ $((`date "+%s"` - `date -d "$(egrep -v '
            '"(Processing by Api::ApiController\#index as JSON|Started GET "/api" for '
            '127.0.0.1|Completed 200 OK in)" /var/www/miq/vmdb/log/production.log | tail -1 |cut '
            '-d"[" -f3 | cut -d"]" -f1 | cut -d" " -f1)\" \"+%s\"`)) -lt {} ];'
            'then echo "False";'
            'else echo "True";'
            'fi;'.format(idle_time))
        return True if 'True' in ssh_output else False

    @cached_property
    def build_datetime(self):
        datetime = self.ssh_client.get_build_datetime()
        return datetime

    @cached_property
    def build_date(self):
        date = self.ssh_client.get_build_date()
        return date

    @cached_property
    def is_downstream(self):
        return self.ssh_client.is_appliance_downstream()

    def has_netapp(self):
        return self.ssh_client.appliance_has_netapp()

    @cached_property
    def guid(self):
        result = self.ssh_client.run_command('cat /var/www/miq/vmdb/GUID')
        return result.output

    @cached_property
    def evm_id(self):
        miq_servers = self.db['miq_servers']
        return self.db.session.query(miq_servers.id).filter(miq_servers.guid == self.guid)[0][0]

    @property
    def server_roles(self):
        """Return a dictionary of server roles from database"""
        asr = self.db['assigned_server_roles']
        sr = self.db['server_roles']
        all_role_names = {row[0] for row in self.db.session.query(sr.name)}
        # Query all active server roles assigned to this server
        query = self.db.session\
            .query(sr.name)\
            .join(asr, asr.server_role_id == sr.id)\
            .filter(asr.miq_server_id == self.evm_id)\
            .filter(asr.active == True)  # noqa
        active_roles = {row[0] for row in query}
        roles = {role_name: role_name in active_roles for role_name in all_role_names}
        dead_keys = ['database_owner', 'vdi_inventory']
        for key in roles:
            if not self.is_storage_enabled:
                if key.startswith('storage'):
                    dead_keys.append(key)
                if key == 'vmdb_storage_bridge':
                    dead_keys.append(key)
        for key in dead_keys:
            try:
                del roles[key]
            except KeyError:
                pass
        return roles

    @server_roles.setter
    def server_roles(self, roles):
        """Sets the server roles. Requires a dictionary full of the role keys with bool values."""
        if self.server_roles == roles:
            self.log.debug(' Roles already match, returning...')
            return
        yaml = self.get_yaml_config()
        yaml['server']['role'] = ','.join([role for role, boolean in roles.iteritems() if boolean])
        self.set_yaml_config(yaml)
        wait_for(lambda: self.server_roles == roles, num_sec=300, delay=15)

    @cached_property
    def configuration_details(self):
        """Return details that are necessary to navigate through Configuration accordions.

        Args:
            ip_address: IP address of the server to match. If None, uses hostname from
                ``conf.env['base_url']``

        Returns:
            If the data weren't found in the DB, :py:class:`NoneType`
            If the data were found, it returns tuple ``(region, server name,
            server id, server zone id)``
        """
        try:
            miq_servers = self.db['miq_servers']
            for region in self.db.session.query(self.db['miq_regions']):
                reg_min = region.region * SEQ_FACT
                reg_max = reg_min + SEQ_FACT
                all_servers = self.db.session.query(miq_servers).all()
                server = None
                if len(all_servers) == 1:
                    # If there's only one server, it's the one we want
                    server = all_servers[0]
                else:
                    # Otherwise, filter based on id and ip/guid
                    def server_filter(server):
                        return all([
                            server.id >= reg_min,
                            server.id < reg_max,
                            # second check because of openstack ip addresses
                            server.ipaddress == self.db.hostname or server.guid == self.guid
                        ])
                    servers = filter(server_filter, all_servers)
                    if servers:
                        server = servers[0]
                if server:
                    return region.region, server.name, server.id, server.zone_id
                else:
                    return None, None, None, None
            else:
                return None

        except KeyError:
            return None

    def server_id(self):
        try:
            return self.configuration_details[2]
        except TypeError:
            return None

    def server_region(self):
        try:
            return self.configuration_details[0]
        except TypeError:
            return None

    def server_name(self):
        try:
            return self.configuration_details[1]
        except TypeError:
            return None

    def server_zone_id(self):
        try:
            return self.configuration_details[3]
        except TypeError:
            return None

    def server_region_string(self):
        r = self.server_region()
        return "{} Region: Region {} [{}]".format(
            self.product_name, r, r)

    def slave_server_zone_id(self):
        table = self.db["miq_servers"]
        try:
            return self.db.session.query(table.id).filter(table.is_master == 'false').first()[0]
        except TypeError:
            return None

    def slave_server_name(self):
        table = self.db["miq_servers"]
        try:
            return self.db.session.query(table.name).filter(
                table.id == self.slave_server_zone_id()).first()[0]
        except TypeError:
            return None

    @cached_property
    def company_name(self):
        return self.get_yaml_config()["server"]["company"]

    @cached_property
    def zone_description(self):
        zone_id = self.server_zone_id()
        zones = list(
            self.db.session.query(self.db["zones"]).filter(
                self.db["zones"].id == zone_id
            )
        )
        if zones:
            return zones[0].description
        else:
            return None

    def host_id(self, hostname):
        hosts = list(
            self.db.session.query(self.db["hosts"]).filter(
                self.db["hosts"].name == hostname
            )
        )
        if hosts:
            return str(hosts[0].id)
        else:
            return None

    @cached_property
    def is_storage_enabled(self):
        return 'storage' in self.get_yaml_config().get('product', {})

    def get_yaml_config(self):
        writeout = self.ssh_client.run_rails_command(
            '"File.open(\'/tmp/yam_dump.yaml\', \'w\') '
            '{|f| f.write(Settings.to_hash.deep_stringify_keys.to_yaml) }"'
        )
        if writeout.rc:
            logger.error("Config couldn't be found")
            logger.error(writeout.output)
            raise Exception('Error obtaining config')
        base_data = self.ssh_client.run_command('cat /tmp/yam_dump.yaml')
        if base_data.rc:
            logger.error("Config couldn't be found")
            logger.error(base_data.output)
            raise Exception('Error obtaining config')
        try:
            return yaml.load(base_data.output)
        except:
            logger.debug(base_data.output)
            raise

    def set_yaml_config(self, data_dict):
        temp_yaml = NamedTemporaryFile()
        dest_yaml = '/tmp/conf.yaml'
        yaml.dump(data_dict, temp_yaml, default_flow_style=False)
        self.ssh_client.put_file(temp_yaml.name, dest_yaml)
        # Build and send ruby script
        dest_ruby = '/tmp/set_conf.rb'

        ruby_template = data_path.join('utils', 'cfmedb_set_config.rbt')
        ruby_replacements = {
            'config_file': dest_yaml
        }
        temp_ruby = load_data_file(ruby_template.strpath, ruby_replacements)
        self.ssh_client.put_file(temp_ruby.name, dest_ruby)

        # Run it
        result = self.ssh_client.run_rails_command(dest_ruby)
        if result:
            self.server_details_changed()
        else:
            raise Exception('Unable to set config: {!r}:{!r}'.format(result.rc, result.output))

    def set_session_timeout(self, timeout=86400, quiet=True):
        """Sets the timeout of UI timeout.

        Args:
            timeout: Timeout in seconds
            quiet: Whether to ignore any errors
        """
        try:
            vmdb_config = self.get_yaml_config()
            if vmdb_config["session"]["timeout"] != timeout:
                vmdb_config["session"]["timeout"] = timeout
                self.set_yaml_config(vmdb_config)
        except Exception as ex:
            logger.error('Setting session timeout failed:')
            logger.exception(ex)
            if not quiet:
                raise

    def delete_all_providers(self):
        logger.info('Destroying all appliance providers')
        for prov in self.rest_api.collections.providers:
            prov.action.delete()

    def reset_automate_model(self):
        with self.ssh_client as ssh_client:
            ssh_client.run_rake_command("evm:automate:reset")

    def server_details_changed(self):
        clear_property_cache(self, 'configuration_details', 'zone_description')

    @logger_wrap("Setting dev branch: {}")
    def use_dev_branch(self, repo, branch, log_callback=None):
        """Sets up an exitsing appliance to change the branch to specified one and reset it.

        Args:
            repo: URL to the repo
            branch: Branch of that repo
        """
        with self.ssh_client as ssh_client:
            dev_branch_cmd = 'cd /var/www/miq/vmdb; git remote add dev_branch {}'.format(repo)
            if not ssh_client.run_command(dev_branch_cmd):
                ssh_client.run_command('cd /var/www/miq/vmdb; git remote remove dev_branch')
                if not ssh_client.run_command(dev_branch_cmd):
                    raise Exception('Could not add the dev_branch remote')
            # We now have the repo and now let's update it
            ssh_client.run_command('cd /var/www/miq/vmdb; git remote update')
            self.stop_evm_service()
            ssh_client.run_command(
                'cd /var/www/miq/vmdb; git checkout dev_branch/{}'.format(branch))
            ssh_client.run_command('cd /var/www/miq/vmdb; bin/update')
            self.start_evm_service()
            self.wait_for_evm_service()
            self.wait_for_web_ui()

    def check_domain_enabled(self, domain):
        namespaces = self.db["miq_ae_namespaces"]
        q = self.db.session.query(namespaces).filter(
            namespaces.parent_id == None, namespaces.name == domain)  # NOQA (for is/==)
        try:
            return list(q)[0].enabled
        except IndexError:
            raise KeyError("No such Domain: {}".format(domain))

    def configure_appliance_for_openldap_ext_auth(self, appliance_fqdn):
        """This method changes the /etc/sssd/sssd.conf and /etc/openldap/ldap.conf files to set
            up the appliance for an external authentication with OpenLdap.
            Apache file configurations are updated, for webui to take effect.

           arguments:
                appliance_name: FQDN for the appliance.

        """
        openldap_domain1 = conf.cfme_data['auth_modes']['ext_openldap']
        assert self.ssh_client.run_command('appliance_console_cli --host {}'.format(appliance_fqdn))
        self.ssh_client.run_command('echo "{}\t{}" > /etc/hosts'.format(
            openldap_domain1['ipaddress'], openldap_domain1['hostname']))
        self.ssh_client.put_file(
            local_file=conf_path.join(openldap_domain1['cert_filename']).strpath,
            remote_file=openldap_domain1['cert_filepath'])
        ldap_conf_data = conf.cfme_data['auth_modes']['ext_openldap']['ldap_conf']
        sssd_conf_data = conf.cfme_data['auth_modes']['ext_openldap']['sssd_conf']
        command1 = 'echo "{}"  > /etc/openldap/ldap.conf'.format(ldap_conf_data)
        command2 = 'echo "{}" > /etc/sssd/sssd.conf && chown -R root:root /etc/sssd/sssd.conf && ' \
                   'chmod 600 /etc/sssd/sssd.conf'.format(sssd_conf_data)
        assert self.ssh_client.run_command(command1)
        assert self.ssh_client.run_command(command2)
        template_dir = '/opt/rh/cfme-appliance/TEMPLATE'
        if self.version == 'master':
            template_dir = '/var/www/miq/system/TEMPLATE'
        httpd_auth = '/etc/pam.d/httpd-auth'
        manageiq_ext_auth = '/etc/httpd/conf.d/manageiq-external-auth.conf'
        apache_config = """
        cp {template_dir}/etc/pam.d/httpd-auth  {httpd_auth} &&
        cp {template_dir}/etc/httpd/conf.d/manageiq-remote-user.conf /etc/httpd/conf.d/ &&
        cp {template_dir}/etc/httpd/conf.d/manageiq-external-auth.conf.erb {manageiq_ext_auth}
    """.format(template_dir=template_dir, httpd_auth=httpd_auth,
               manageiq_ext_auth=manageiq_ext_auth)
        assert self.ssh_client.run_command(apache_config)
        self.ssh_client.run_command(
            'setenforce 0 && systemctl restart sssd && systemctl restart httpd')
        self.wait_for_web_ui()

    @logger_wrap("Configuring VM Console: {}")
    def configure_vm_console_cert(self, log_callback=None):
        """This method generates a self signed SSL cert and installs it
           in the miq/vmdb/certs dir.   This cert will be used by the
           HTML 5 VM Console feature.  Note evmserverd needs to be restarted
           after running this.
        """
        log_callback('Installing SSL certificate')

        cert = conf.cfme_data['vm_console'].get('cert')
        if cert is None:
            raise Exception('vm_console:cert does not exist in cfme_data.yaml')

        cert_file = os.path.join(cert.install_dir, 'server.cer')
        key_file = os.path.join(cert.install_dir, 'server.cer.key')
        cert_generator = scripts_path.join('gen_ssl_cert.py').strpath
        remote_cert_generator = os.path.join('/usr/bin', 'gen_ssl_cert.py')

        # Copy self signed SSL certificate generator to the appliance
        # because it needs to get the FQDN for the cert it generates.
        self.ssh_client.put_file(cert_generator, remote_cert_generator)

        # Generate cert
        command = '''
            {cert_generator} \\
                --C="{country}" \\
                --ST="{state}" \\
                --L="{city}" \\
                --O="{organization}" \\
                --OU="{organizational_unit}" \\
                --keyFile="{key}" \\
                --certFile="{cert}"
        '''.format(
            cert_generator=remote_cert_generator,
            country=cert.country,
            state=cert.state,
            city=cert.city,
            organization=cert.organization,
            organizational_unit=cert.organizational_unit,
            key=key_file,
            cert=cert_file,
        )
        result = self.ssh_client.run_command(command)
        if not result == 0:
            raise Exception(
                'Failed to generate self-signed SSL cert on appliance: {}'.format(
                    result[1]
                )
            )


class Appliance(IPAppliance):
    """Appliance represents an already provisioned cfme appliance vm

    Args:
        provider_name: Name of the provider this appliance is running under
        vm_name: Name of the VM this appliance is running as
        browser_steal: Setting of the browser_steal attribute.
    """

    _default_name = 'EVM'

    def __init__(self, provider_name, vm_name, browser_steal=False, container=None):
        """Initializes a deployed appliance VM
        """
        super(Appliance, self).__init__(browser_steal=browser_steal, container=None)
        self.name = Appliance._default_name

        self._provider_key = provider_name
        self.vmname = vm_name

    def __eq__(self, other):
        return isinstance(other, type(self)) and (
            self.vmname == other.vmname and self._provider_key == other._provider_key)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash((self.vmname, self._provider_key))

    @property
    def ipapp(self):
        # For backwards compat
        return self

    @cached_property
    def provider(self):
        """
        Note:
            Cannot be cached because provider object is unpickable.
        """
        from utils.providers import get_mgmt
        return get_mgmt(self._provider_key)

    @property
    def vm_name(self):
        """ VM's name of the appliance on the provider """
        return self.vmname

    @cached_property
    def address(self):
        def is_ip_available():
            try:
                ip = self.provider.get_ip_address(self.vm_name)
                if ip is None:
                    return False
                else:
                    return ip
            except AttributeError:
                return False

        ec, tc = wait_for(is_ip_available,
                          delay=5,
                          num_sec=600)
        return str(ec)

    def _custom_configure(self, **kwargs):
        log_callback = kwargs.pop(
            "log_callback",
            lambda msg: logger.info("Custom configure %s: %s", self.vmname, msg))
        region = kwargs.get('region', 0)
        db_address = kwargs.get('db_address', None)
        key_address = kwargs.get('key_address', None)
        db_username = kwargs.get('db_username', None)
        db_password = kwargs.get('ssh_password', None)
        ssh_password = kwargs.get('ssh_password', None)
        db_name = kwargs.get('db_name', None)

        if kwargs.get('fix_ntp_clock', True) is True:
            self.fix_ntp_clock(log_callback=log_callback)
        if kwargs.get('db_address', None) is None:
            self.enable_internal_db(
                region, key_address, db_password, ssh_password, log_callback=log_callback)
        else:
            self.enable_external_db(
                db_address, region, db_name, db_username, db_password,
                log_callback=log_callback)
        self.wait_for_web_ui(timeout=1800, log_callback=log_callback)
        if kwargs.get('loosen_pgssl', True) is True:
            self.loosen_pgssl(log_callback=log_callback)

        name_to_set = kwargs.get('name_to_set', None)
        if name_to_set is not None and name_to_set != self.name:
            self.rename(name_to_set)
            self.restart_evm_service(log_callback=log_callback)
            self.wait_for_web_ui(log_callback=log_callback)

    @logger_wrap("Configure Appliance: {}")
    def configure(self, setup_fleece=False, log_callback=None, **kwargs):
        """Configures appliance - database setup, rename, ntp sync

        Utility method to make things easier.

        Args:
            db_address: Address of external database if set, internal database if ``None``
                        (default ``None``)
            name_to_set: Name to set the appliance name to if not ``None`` (default ``None``)
            region: Number to assign to region (default ``0``)
            fix_ntp_clock: Fixes appliance time if ``True`` (default ``True``)
            loosen_pgssl: Loosens postgres connections if ``True`` (default ``True``)
            key_address: Fetch encryption key from this address if set, generate a new key if
                         ``None`` (default ``None``)

        """
        log_callback("Configuring appliance {} on {}".format(self.vmname, self._provider_key))
        if kwargs:
            with self:
                self._custom_configure(**kwargs)
        else:
            # Defer to the IPAppliance.
            super(Appliance, self).configure(log_callback=log_callback)
        # And do configure the fleecing if requested
        if setup_fleece:
            self.configure_fleecing(log_callback=log_callback)

    @logger_wrap("Configure fleecing: {}")
    def configure_fleecing(self, log_callback=None):
        from cfme.configure.configuration import set_server_roles, get_server_roles
        with self(browser_steal=True):
            if self.is_on_vsphere:
                self.install_vddk(reboot=True, log_callback=log_callback)
                self.wait_for_web_ui(log_callback=log_callback)

            if self.is_on_rhev:
                self.add_rhev_direct_lun_disk()

            log_callback('Enabling smart proxy role...')
            roles = get_server_roles()
            if not roles["smartproxy"]:
                roles["smartproxy"] = True
                set_server_roles(**roles)
                # web ui crashes
                if str(self.version).startswith("5.2.5") or str(self.version).startswith("5.5"):
                    try:
                        self.wait_for_web_ui(timeout=300, running=False)
                    except:
                        pass
                    self.wait_for_web_ui(running=True)

            # add provider
            log_callback('Setting up provider...')
            self.provider.setup()

            # credential hosts
            log_callback('Credentialing hosts...')
            if not RUNNING_UNDER_SPROUT:
                from utils.hosts import setup_providers_hosts_credentials
            setup_providers_hosts_credentials(self._provider_key, ignore_errors=True)

            # if rhev, set relationship
            if self.is_on_rhev:
                from cfme.infrastructure.virtual_machines import Vm  # For Vm.CfmeRelationship
                log_callback('Setting up CFME VM relationship...')
                from cfme.common.vm import VM
                from utils.providers import get_crud
                vm = VM.factory(self.vm_name, get_crud(self._provider_key))
                cfme_rel = Vm.CfmeRelationship(vm)
                cfme_rel.set_relationship(str(self.server_name()), self.server_id())

    def does_vm_exist(self):
        return self.provider.does_vm_exist(self.vm_name)

    def rename(self, new_name):
        """Changes appliance name

        Args:
            new_name: Name to set

        Note:
            Database must be up and running and evm service must be (re)started afterwards
            for the name change to take effect.
        """
        vmdb_config = self.get_yaml_config()
        vmdb_config['server']['name'] = new_name
        self.set_yaml_config(vmdb_config)
        self.name = new_name

    def destroy(self):
        """Destroys the VM this appliance is running as
        """
        if self.is_on_rhev:
            # if rhev, try to remove direct_lun just in case it is detach
            self.remove_rhev_direct_lun_disk()
        self.provider.delete_vm(self.vm_name)

    def stop(self):
        """Stops the VM this appliance is running as
        """
        self.provider.stop_vm(self.vm_name)
        self.provider.wait_vm_stopped(self.vm_name)

    def start(self):
        """Starts the VM this appliance is running as
        """
        self.provider.start_vm(self.vm_name)
        self.provider.wait_vm_running(self.vm_name)

    def templatize(self, seal=True):
        """Marks the appliance as a template. Destroys the original VM in the process.

        By default it runs the sealing process. If you have done it differently, you can opt out.

        Args:
            seal: Whether to run the sealing process (making the VM 'universal').
        """
        if seal:
            if not self.is_running:
                self.start()
            self.seal_for_templatizing()
            self.stop()
        else:
            if self.is_running:
                self.stop()
        self.provider.mark_as_template(self.vm_name)

    @property
    def is_running(self):
        return self.provider.is_vm_running(self.vm_name)

    @property
    def is_on_rhev(self):
        from cfme.infrastructure.provider.rhevm import RHEVMProvider
        return isinstance(self.provider, RHEVMProvider.mgmt_class)

    @property
    def is_on_vsphere(self):
        from cfme.infrastructure.provider.virtualcenter import VMwareProvider
        return isinstance(self.provider, VMwareProvider.mgmt_class)

    def add_rhev_direct_lun_disk(self, log_callback=None):
        if log_callback is None:
            log_callback = logger.info
        if not self.is_on_rhev:
            log_callback("appliance NOT on rhev, unable to connect direct_lun")
            raise ApplianceException("appliance NOT on rhev, unable to connect direct_lun")
        log_callback('Adding RHEV direct_lun hook...')
        self.wait_for_ssh()
        try:
            self.provider.connect_direct_lun_to_appliance(self.vm_name, False)
        except Exception as e:
            log_callback("Appliance {} failed to connect RHEV direct LUN.".format(self.vm_name))
            log_callback(str(e))
            raise

    @logger_wrap("Remove RHEV LUN: {}")
    def remove_rhev_direct_lun_disk(self, log_callback=None):
        if not self.is_on_rhev:
            msg = "appliance {} NOT on rhev, unable to disconnect direct_lun".format(self.vmname)
            log_callback(msg)
            raise ApplianceException(msg)
        log_callback('Removing RHEV direct_lun hook...')
        self.wait_for_ssh()
        try:
            self.provider.connect_direct_lun_to_appliance(self.vm_name, True)
        except Exception as e:
            log_callback("Appliance {} failed to connect RHEV direct LUN.".format(self.vm_name))
            log_callback(str(e))
            raise


def provision_appliance(version=None, vm_name_prefix='cfme', template=None, provider_name=None,
                        vm_name=None):
    """Provisions fresh, unconfigured appliance of a specific version

    Note:
        Version must be mapped to template name under ``appliance_provisioning > versions``
        in ``cfme_data.yaml``.
        If no matching template for given version is found, and trackerbot is set up,
        the latest available template of the same stream will be used.
        E.g.: if there is no template for 5.5.5.1 but there is 5.5.5.3, it will be used instead.
        If both template name and version are specified, template name takes priority.

    Args:
        version: version of appliance to provision
        vm_name_prefix: name prefix to use when deploying the appliance vm

    Returns: Unconfigured appliance; instance of :py:class:`Appliance`

    Usage:
        my_appliance = provision_appliance('5.5.1.8', 'my_tests')
        my_appliance.fix_ntp_clock()
        ...other configuration...
        my_appliance.enable_internal_db()
        my_appliance.wait_for_web_ui()
        or
        my_appliance = provision_appliance('5.5.1.8', 'my_tests')
        my_appliance.configure()
    """

    def _generate_vm_name():
        if version is not None:
            version_digits = ''.join([letter for letter in version if letter.isdigit()])
            return '{}_{}_{}'.format(
                vm_name_prefix, version_digits, fauxfactory.gen_alphanumeric(8))
        else:
            return '{}_{}'.format(vm_name_prefix, fauxfactory.gen_alphanumeric(8))

    def _get_latest_template():
        from utils import trackerbot
        api = trackerbot.api()
        stream = get_stream(version)
        template_data = trackerbot.latest_template(api, stream, provider_name)
        return template_data.get('latest_template', None)

    if provider_name is None:
        provider_name = conf.cfme_data.get('appliance_provisioning', {})['default_provider']

    if template is not None:
        template_name = template
    elif version is not None:
        templates_by_version = conf.cfme_data.get('appliance_provisioning', {}).get('versions', {})
        try:
            template_name = templates_by_version[version]
        except KeyError:
            # We try to get the latest template from the same stream - if trackerbot is set up
            if conf.env.get('trackerbot', {}):
                template_name = _get_latest_template()
                if not template_name:
                    raise ApplianceException('No template found for stream {} on provider {}'
                        .format(get_stream(version), provider_name))
                logger.warning('No template found matching version %s, using %s instead.',
                    version, template_name)
            else:
                raise ApplianceException('No template found matching version {}'.format(version))
    else:
        raise ApplianceException('Either version or template name must be specified')

    prov_data = conf.cfme_data.get('management_systems', {})[provider_name]
    from utils.providers import get_mgmt
    provider = get_mgmt(provider_name)
    if not vm_name:
        vm_name = _generate_vm_name()

    deploy_args = {}
    deploy_args['vm_name'] = vm_name

    if prov_data['type'] == 'rhevm':
        deploy_args['cluster'] = prov_data['default_cluster']

    if prov_data["type"] == "virtualcenter":
        if "allowed_datastores" in prov_data:
            deploy_args["allowed_datastores"] = prov_data["allowed_datastores"]

    provider.deploy_template(template_name, **deploy_args)

    return Appliance(provider_name, vm_name)


class ApplianceStack(LocalStack):

    def push(self, obj):
        was_before = self.top
        super(ApplianceStack, self).push(obj)

        logger.info("Pushed appliance {} on stack (was {} before) ".format(
            obj.address, getattr(was_before, 'address', 'empty')))
        if obj.browser_steal:
            from utils import browser
            browser.start()

    def pop(self):
        was_before = super(ApplianceStack, self).pop()
        current = self.top
        logger.info(
            "Popped appliance {} from the stack (now there is {})".format(
                was_before.address, getattr(current, 'address', 'empty')))
        if was_before.browser_steal:
            from utils import browser
            browser.start()
        return was_before


stack = ApplianceStack()


def get_or_create_current_appliance():
    if stack.top is None:
        base_url = conf.env['base_url']
        if base_url is None or str(base_url.lower()) == 'none':
            raise ValueError('No IP address specified! Specified: {}'.format(repr(base_url)))
        openshift_creds = conf.env.get('openshift', {})
        db_host = conf.env.get('db_host', None)
        if not isinstance(openshift_creds, dict):
            raise TypeError('The openshift entry in env.yaml must be a dictionary')
        stack.push(
            IPAppliance(
                urlparse(base_url),
                container=conf.env.get('container', None),
                openshift_creds=openshift_creds, db_host=db_host))
    return stack.top


current_appliance = LocalProxy(get_or_create_current_appliance)


class CurrentAppliance(object):
    def __get__(self, instance, owner):
        return get_or_create_current_appliance()


class Navigatable(object):

    appliance = CurrentAppliance()

    def __init__(self, appliance=None):
        self.appliance = appliance or get_or_create_current_appliance()

    @property
    def browser(self):
        return self.appliance.browser.widgetastic

    def create_view(self, view_class, o=None, override=None):
        o = o or self
        if override is not None:
            new_obj = copy(o)
            new_obj.__dict__.update(override)
        else:
            new_obj = o
        return self.appliance.browser.create_view(
            view_class, additional_context={'object': new_obj})
