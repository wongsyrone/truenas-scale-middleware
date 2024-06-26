import datetime
import enum
import errno
import json
import ipaddress
import os
import contextlib

from middlewared.plugins.smb import SMBCmd, SMBPath
from middlewared.plugins.kerberos import krb5ccache
from middlewared.schema import (
    accepts, Bool, Dict, Int, IPAddr, LDAP_DN, List, NetbiosName, Ref, returns, Str
)
from middlewared.service import job, private, ConfigService, ValidationError, ValidationErrors
from middlewared.service_exception import CallError, MatchNotFound
import middlewared.sqlalchemy as sa
from middlewared.utils import run
from middlewared.utils.directoryservices.krb5_error import KRB5ErrCode, KRB5Error
from middlewared.plugins.directoryservices import DSStatus
from middlewared.plugins.idmap import DSType
from middlewared.validators import Range


class neterr(enum.Enum):
    JOINED = 1
    NOTJOINED = 2
    FAULT = 3

    def to_status(errstr):
        errors_to_rejoin = [
            '0xfffffff6',
            'LDAP_INVALID_CREDENTIALS',
            'The name provided is not a properly formed account name',
            'The attempted logon is invalid.'
        ]
        for err in errors_to_rejoin:
            if err in errstr:
                return neterr.NOTJOINED

        return neterr.FAULT


class ActiveDirectoryModel(sa.Model):
    __tablename__ = 'directoryservice_activedirectory'

    id = sa.Column(sa.Integer(), primary_key=True)
    ad_domainname = sa.Column(sa.String(120))
    ad_bindname = sa.Column(sa.String(120))
    ad_verbose_logging = sa.Column(sa.Boolean())
    ad_allow_trusted_doms = sa.Column(sa.Boolean())
    ad_use_default_domain = sa.Column(sa.Boolean())
    ad_allow_dns_updates = sa.Column(sa.Boolean())
    ad_disable_freenas_cache = sa.Column(sa.Boolean())
    ad_restrict_pam = sa.Column(sa.Boolean())
    ad_site = sa.Column(sa.String(120), nullable=True)
    ad_timeout = sa.Column(sa.Integer())
    ad_dns_timeout = sa.Column(sa.Integer())
    ad_nss_info = sa.Column(sa.String(120), nullable=True)
    ad_enable = sa.Column(sa.Boolean())
    ad_kerberos_realm_id = sa.Column(sa.ForeignKey('directoryservice_kerberosrealm.id', ondelete='SET NULL'),
                                     index=True, nullable=True)
    ad_kerberos_principal = sa.Column(sa.String(255))
    ad_createcomputer = sa.Column(sa.String(255))


class ActiveDirectoryService(ConfigService):

    class Config:
        service = "activedirectory"
        datastore = 'directoryservice.activedirectory'
        datastore_extend = "activedirectory.ad_extend"
        datastore_prefix = "ad_"
        cli_namespace = "directory_service.activedirectory"
        role_prefix = "DIRECTORY_SERVICE"

    ENTRY = Dict(
        'activedirectory_update',
        Str('domainname', required=True),
        Str('bindname'),
        Str('bindpw', private=True),
        Bool('verbose_logging'),
        Bool('use_default_domain'),
        Bool('allow_trusted_doms'),
        Bool('allow_dns_updates'),
        Bool('disable_freenas_cache'),
        Bool('restrict_pam', default=False),
        Str('site', null=True),
        Int('kerberos_realm', null=True),
        Str('kerberos_principal', null=True),
        Int('timeout', default=60),
        Int('dns_timeout', default=10, validators=[Range(min_=5, max_=40)]),
        Str('nss_info', null=True, enum=['TEMPLATE', 'SFU', 'SFU20', 'RFC2307']),
        Str('createcomputer'),
        NetbiosName('netbiosname'),
        NetbiosName('netbiosname_b'),
        List('netbiosalias', items=[NetbiosName('alias')]),
        Bool('enable'),
        register=True
    )

    @private
    async def ad_extend(self, ad):
        smb = await self.middleware.call('smb.config')

        ad.update({
            'netbiosname': smb['netbiosname_local'],
            'netbiosalias': smb['netbiosalias']
        })

        if ad.get('nss_info'):
            ad['nss_info'] = ad['nss_info'].upper()
        else:
            ad['nss_info'] = 'TEMPLATE'

        if ad.get('kerberos_realm') and type(ad['kerberos_realm']) is dict:
            ad['kerberos_realm'] = ad['kerberos_realm']['id']

        return ad

    @private
    async def ad_compress(self, ad):
        """
        Convert kerberos realm to id. Force domain to upper-case. Remove
        foreign entries.
        kinit will fail if domain name is lower-case.
        """
        for key in ['netbiosname', 'netbiosname_b', 'netbiosalias', 'bindpw']:
            if key in ad:
                ad.pop(key)

        if ad.get('nss_info'):
            ad['nss_info'] = ad['nss_info'].upper()

        return ad

    @accepts()
    @returns(Ref('nss_info_ad'))
    async def nss_info_choices(self):
        """
        Returns list of available LDAP schema choices.
        """
        return await self.middleware.call('directoryservices.nss_info_choices', 'ACTIVEDIRECTORY')

    @private
    async def update_netbios_data(self, old, new):
        must_update = False
        for key in ['netbiosname', 'netbiosalias']:
            if key in new and old[key] != new[key]:
                if old['enable']:
                    raise ValidationError(
                        f'activedirectory.{key}',
                        'NetBIOS names may not be changed while service is enabled.'
                    )

                must_update = True
                break

        if not must_update:
            return

        await self.middleware.call('smb.update', {
            'netbiosname': new['netbiosname'],
            'netbiosalias': new['netbiosalias']
        })

    @private
    async def common_validate(self, new, old, verrors):
        try:
            if not (await self.middleware.call('activedirectory.netbiosname_is_ours', new['netbiosname'], new['domainname'], new['dns_timeout'])):
                verrors.add(
                    'activedirectory_update.netbiosname',
                    f'NetBIOS name [{new["netbiosname"]}] appears to be in use by another computer in Active Directory DNS. '
                    'Further investigation and DNS corrections will be required prior to using the aforementioned name to '
                    'join Active Directory.'
                )
        except CallError:
            pass

        if new['kerberos_realm'] and new['kerberos_realm'] != old['kerberos_realm']:
            realm = await self.middleware.call('kerberos.realm.query', [("id", "=", new['kerberos_realm'])])
            if not realm:
                verrors.add(
                    'activedirectory_update.kerberos_realm',
                    'Invalid Kerberos realm id. Realm does not exist.'
                )

        if not new["enable"]:
            return

        if not await self.middleware.call('pool.query', [], {'count': True}):
            verrors.add(
                "activedirectory_update.enable",
                "Active Directory service may not be enabled before data pool is created."
            )
        ldap_enabled = (await self.middleware.call('ldap.config'))['enable']
        if ldap_enabled:
            verrors.add(
                "activedirectory_update.enable",
                "Active Directory service may not be enabled while LDAP service is enabled."
            )
        if new["enable"] and old["enable"] and new["kerberos_realm"] != old["kerberos_realm"]:
            verrors.add(
                "activedirectory_update.kerberos_realm",
                "Kerberos realm may not be altered while the AD service is enabled. "
                "This is to avoid introducing possible configuration errors that may result "
                "in a production outage."
            )
        if not new.get("bindpw") and not new["kerberos_principal"]:
            verrors.add(
                "activedirectory_update.bindname",
                "Bind credentials or kerberos keytab are required to join an AD domain."
            )
        if new.get("bindpw") and new["kerberos_principal"]:
            verrors.add(
                "activedirectory_update.kerberos_principal",
                "Simultaneous keytab and password authentication are not permitted."
            )
        if not new["domainname"]:
            verrors.add(
                "activedirectory_update.domainname",
                "AD domain name is required."
            )

        if new['allow_dns_updates']:
            ha_mode = await self.middleware.call('smb.get_smb_ha_mode')

            if ha_mode == 'UNIFIED':
                if await self.middleware.call('failover.status') != 'MASTER':
                    return

            smb = await self.middleware.call('smb.config')
            addresses = await self.middleware.call(
                'activedirectory.get_ipaddresses', new, smb, ha_mode
            )

            if not addresses:
                verrors.add(
                    'activedirectory_update.allow_dns_updates',
                    'No server IP addresses passed DNS validation. '
                    'This may indicate an improperly configured reverse zone. '
                    'Review middleware log files for details regarding errors encountered.',
                )

            for a in addresses:
                addr = ipaddress.ip_address(a)
                if addr.is_reserved:
                    verrors.add(
                        'activedirectory_update.allow_dns_updates',
                        f'{addr}: automatic DNS update would result in registering a reserved '
                        'IP address. Users may disable automatic DNS updates and manually '
                        'configure DNS A and AAAA records as needed for their domain.'
                    )

                if addr.is_loopback:
                    verrors.add(
                        'activedirectory_update.allow_dns_updates',
                        f'{addr}: automatic DNS update would result in registering a loopback '
                        'address. Users may disable automatic DNS updates and manually '
                        'configure DNS A and AAAA records as needed for their domain.'
                    )

                if addr.is_link_local:
                    verrors.add(
                        'activedirectory_update.allow_dns_updates',
                        f'{addr}: automatic DNS update would result in registering a link-local '
                        'address. Users may disable automatic DNS updates and manually '
                        'configure DNS A and AAAA records as needed for their domain.'
                    )

                if addr.is_multicast:
                    verrors.add(
                        'activedirectory_update.allow_dns_updates',
                        f'{addr}: automatic DNS update would result in registering a multicast '
                        'address. Users may disable automatic DNS updates and manually '
                        'configure DNS A and AAAA records as needed for their domain.'
                    )

    @accepts(Ref('activedirectory_update'))
    @returns(Ref('activedirectory_update'))
    @job(lock="AD_start_stop")
    async def do_update(self, job, data):
        """
        Update active directory configuration.
        `domainname` full DNS domain name of the Active Directory domain.

        `bindname` username used to perform the intial domain join.

        `bindpw` password used to perform the initial domain join. User-
        provided credentials are used to obtain a kerberos ticket, which
        is used to perform the actual domain join.

        `verbose_logging` increase logging during the domain join process.

        `use_default_domain` controls whether domain users and groups have
        the pre-windows 2000 domain name prepended to the user account. When
        enabled, the user appears as "administrator" rather than
        "EXAMPLE\administrator"

        `allow_trusted_doms` enable support for trusted domains. If this
        parameter is enabled, then separate idmap backends _must_ be configured
        for each trusted domain, and the idmap cache should be cleared.

        `allow_dns_updates` during the domain join process, automatically
        generate DNS entries in the AD domain for the NAS. If this is disabled,
        then a domain administrator must manually add appropriate DNS entries
        for the NAS. This parameter is recommended for TrueNAS HA servers.

        `disable_freenas_cache` disables active caching of AD users and groups.
        When disabled, only users cached in winbind's internal cache are
        visible in GUI dropdowns. Disabling active caching is recommended
        in environments with a large amount of users.

        `site` AD site of which the NAS is a member. This parameter is auto-
        detected during the domain join process. If no AD site is configured
        for the subnet in which the NAS is configured, then this parameter
        appears as 'Default-First-Site-Name'. Auto-detection is only performed
        during the initial domain join.

        `kerberos_realm` in which the server is located. This parameter is
        automatically populated during the initial domain join. If the NAS has
        an AD site configured and that site has multiple kerberos servers, then
        the kerberos realm is automatically updated with a site-specific
        configuration to use those servers. Auto-detection is only performed
        during initial domain join.

        `kerberos_principal` kerberos principal to use for AD-related
        operations outside of Samba. After intial domain join, this field is
        updated with the kerberos principal associated with the AD machine
        account for the NAS.

        `nss_info` controls how Winbind retrieves Name Service Information to
        construct a user's home directory and login shell. This parameter
        is only effective if the Active Directory Domain Controller supports
        the Microsoft Services for Unix (SFU) LDAP schema.

        `timeout` timeout value for winbind-related operations. This value may
        need to be increased in  environments with high latencies for
        communications with domain controllers or a large number of domain
        controllers. Lowering the value may cause status checks to fail.

        `dns_timeout` timeout value for DNS queries during the initial domain
        join. This value is also set as the NETWORK_TIMEOUT in the ldap config
        file.

        `createcomputer` Active Directory Organizational Unit in which new
        computer accounts are created.

        The OU string is read from top to bottom without RDNs. Slashes ("/")
        are used as delimiters, like `Computers/Servers/NAS`. The backslash
        ("\\") is used to escape characters but not as a separator. Backslashes
        are interpreted at multiple levels and might require doubling or even
        quadrupling to take effect.

        When this field is blank, new computer accounts are created in the
        Active Directory default OU.

        The Active Directory service is started after a configuration
        update if the service was initially disabled, and the updated
        configuration sets `enable` to `True`. The Active Directory
        service is stopped if `enable` is changed to `False`. If the
        configuration is updated, but the initial `enable` state is `True`, and
        remains unchanged, then the samba server is only restarted.

        During the domain join, a kerberos keytab for the newly-created AD
        machine account is generated. It is used for all future
        LDAP / AD interaction and the user-provided credentials are removed.
        """
        verrors = ValidationErrors()
        old = await self.config()
        new = old.copy()
        new.update(data)
        new['domainname'] = new['domainname'].upper()

        try:
            await self.update_netbios_data(old, new)
        except Exception as e:
            raise ValidationError('activedirectory_update.netbiosname', str(e))

        await self.common_validate(new, old, verrors)

        verrors.check()

        if new['enable']:
            if new['allow_trusted_doms'] and not await self.middleware.call('idmap.may_enable_trusted_domains'):
                raise ValidationError(
                    'activedirectory.allow_trusted_doms',
                    'Configuration for trusted domains requires that the idmap backend '
                    'be configured to handle these domains. There are two possible strategies to '
                    'achieve this. The first strategy is to use the AUTORID backend for the domain '
                    'to which TrueNAS is joined. The second strategy is to separately configure idmap '
                    'ranges for every domain that has a trust relationship with the domain to which '
                    'TrueNAS is joined and which has accounts that will be used on the TrueNAS server. '
                    'NOTE: the topic of how to properly map Windows SIDs to Unix IDs is complex and '
                    'may require consultation with administrators of other Unix servers in the '
                    'Active Directory domain to properly coordinate a comprehensive ID mapping strategy.'
                )
            if await self.middleware.call('failover.licensed'):
                if await self.middleware.call('systemdataset.is_boot_pool'):
                    raise ValidationError(
                        'activedirectory.enable',
                        'Active Directory may not be enabled while '
                        'system dataset is on the boot pool'
                    )

        if new['enable'] and old['enable']:
            permitted_keys = [
                'verbose_logging',
                'use_default_domain',
                'allow_trusted_doms',
                'disable_freenas_cache',
                'restrict_pam',
                'timeout',
                'dns_timeout'
            ]
            for entry in old.keys():
                if entry not in new or entry in permitted_keys:
                    continue

                if new[entry] != old[entry]:
                    raise ValidationError(
                        f'activedirectory.{entry}',
                        'Parameter may not be changed while the Active Directory service is enabled.'
                    )

        elif new['enable'] and not old['enable']:
            """
            Currently run two health checks prior to validating domain.
            1) Attempt to kinit with user-provided credentials. This is used to
               verify that the credentials are correct.
            2) Check for an overly large time offset. System kerberos libraries
               may not report the time offset as an error during kinit, but the large
               time offset will prevent libads from using the ticket for the domain
               join.
            """
            try:
                domain_info = await self.domain_info(new['domainname'])
            except CallError as e:
                raise ValidationError('activedirectory.domainname', e.errmsg)

            if abs(domain_info['Server time offset']) > 180:
                raise ValidationError(
                    'activedirectory.domainname',
                    'Time offset from Active Directory domain exceeds maximum '
                    'permitted value. This may indicate an NTP misconfiguration.'
                )

            try:
                await self.middleware.call(
                    'activedirectory.check_nameservers',
                    new['domainname'],
                    new['site'],
                    new['dns_timeout']
                )
            except CallError as e:
                raise ValidationError(
                    'activedirectory.domainname',
                    e.errmsg
                )

            try:
                await self.validate_credentials(new, domain_info['KDC server'])
            except KRB5Error as e:
                # initially assume the validation error will be
                # about the actual password used
                if new['kerberos_principal']:
                    key = 'activedirectory.kerberos_principal'
                else:
                    key = 'activedirectory.bindpw'

                match e.krb5_code:
                    case KRB5ErrCode.KRB5_LIBOS_CANTREADPWD:
                        if key == 'activedirectory.kerberos_principal':
                            msg = 'Kerberos keytab is no longer valid.'
                        else:
                            msg = f'Active Directory account password for user {new["bindname"]} is expired.'
                    case KRB5ErrCode.KRB5KDC_ERR_CLIENT_REVOKED:
                        msg = 'Active Directory account is locked.'
                    case KRB5ErrCode.KRB5_CC_NOTFOUND:
                        if key == 'activedirectory.kerberos_principal':
                            # When we kinit we try to regenerate keytab if the principal
                            # isn't present in it. If we hit this point it means that user
                            # has been tweaking the system-managed keytab in interesting ways.
                            choices = await self.middleware.call(
                                'kerberos.keytab.kerberos_principal_choices'
                            )
                            msg = (
                                'System keytab lacks an entry for the specified kerberos principal. '
                                f'Please select a valid kerberos principal from available choices: {", ".join(choices)}'
                            )
                        else:
                            # This error shouldn't occur if we're trying to get ticket
                            # with username + password combination
                            msg = str(e)
                    case KRB5ErrCode.KRB5KDC_ERR_POLICY:
                        msg = (
                            'Active Directory security policy rejected request to obtain kerberos ticket. '
                            'This may occur if the bind account has been configured to deny interactive '
                            'logons or require two-factor authentication. Depending on organizational '
                            'security policies, one may be required to pre-generate a kerberos keytab '
                            'and upload to TrueNAS server for use during join process.'
                        )
                    case KRB5ErrCode.KRB5KDC_ERR_C_PRINCIPAL_UNKNOWN:
                        # We're dealing with a missing account
                        if key == "activedirectory.bindpw":
                            key = "activedirectory.bindname"

                        msg = (
                            'Client\'s credentials were not found on remote domain controller. The most '
                            'common reasons for the domain controller to return this response is due to a '
                            'typo in the service account name or the service or the computer account being '
                            'deleted from Active Directory.'
                        )
                    case KRB5ErrCode.KRB5KRB_AP_ERR_SKEW:
                        # Domain permitted clock skew may be more restrictive than our basic
                        # check of no greater than 3 minutes.
                        key = 'activedirectory.domainname'
                        msg = (
                            'The time offset between the TrueNAS server and the active directory domain '
                            'controller exceeds the maximum value permitted by the Active Directory '
                            'configuration. This may occur if NTP is improperly configured on the '
                            'TrueNAS server or if the hardware clock on the TrueNAS server is configured '
                            'for a local timezone instead of UTC.'
                        )
                    case KRB5ErrCode.KRB5KDC_ERR_PREAUTH_FAILED:
                        if new['kerberos_principal']:
                            msg = (
                                'Kerberos principal credentials are no longer valid. Rejoining active directory '
                                'may be required.'
                            )
                        else:
                            msg = 'Preauthentication failed. This typically indicates an incorrect bind password.'
                    case _:
                        # Catchall for more kerberos errors. We can expand if needed.
                        msg = str(e)

                raise ValidationError(key, msg)
            except CallError as e:
                # This may be an encapsulated GSSAPI library error
                if e.errno == errno.EINVAL:
                    # special errno set if GSSAPI BadName exception raised
                    if new['kerberos_principal']:
                        raise ValidationError('activedirectory.kerberos_principal', 'Not a valid principal name')
                    else:
                        raise ValidationError('activedirectory.bindname', 'Not a valid username')

                # No meaningful way to convert into a ValidationError, simply re-raise
                raise e from None

        elif not new['enable'] and new.get('bindpw'):
            raise ValidationError(
                'activedirectory.bindpw',
                'The Active Directory bind password is only used when enabling the active '
                'directory service for the first time and is not stored persistently. Therefore it '
                'is only valid when enabling the service.'
            )

        config = await self.ad_compress(new)
        await self.middleware.call('datastore.update', self._config.datastore, new['id'], config, {'prefix': 'ad_'})
        await self.middleware.call('etc.generate', 'smb')

        if not old['enable'] and new['enable']:
            ngc = await self.middleware.call('network.configuration.config')
            if not ngc['domain'] or ngc['domain'] == 'local':
                try:
                    await self.middleware.call(
                        'network.configuration.update',
                        {'domain': new['domainname']}
                    )
                except CallError:
                    self.logger.warning(
                        'Failed to update domain name in network configuration '
                        'to match active directory value of %s', new['domainname'], exc_info=True
                    )

            if not await self.middleware.call(
                'kerberos.check_ticket',
                {'ccache': krb5ccache.SYSTEM.name},
                False
            ):
                await self.middleware.call('kerberos.start')

            try:
                await self.__start(job)
            except Exception as e:
                self.logger.error('Failed to start active directory service. Disabling.')
                await self.set_state(DSStatus['DISABLED'].name)
                await self.middleware.call(
                    'datastore.update', self._config.datastore, new['id'],
                    {'enable': False}, {'prefix': 'ad_'}
                )
                raise e

        elif not new['enable'] and old['enable']:
            await self.__stop(job, new)

        elif new['enable'] and old['enable']:
            await self.middleware.call('service.restart', 'idmap')

        return await self.config()

    @private
    async def set_state(self, state):
        return await self.middleware.call('directoryservices.set_state', {'activedirectory': state})

    @accepts(roles=['DIRECTORY_SERVICE_READ'])
    @returns(Str('directoryservice_state', enum=[x.name for x in DSStatus], register=True))
    async def get_state(self):
        """
        Wrapper function for 'directoryservices.get_state'. Returns only the state of the
        Active Directory service.
        """
        return (await self.middleware.call('directoryservices.get_state'))['activedirectory']

    @private
    async def set_idmap(self, trusted_domains, our_domain):
        idmap = await self.middleware.call('idmap.query',
                                           [('id', '=', DSType.DS_TYPE_ACTIVEDIRECTORY.value)],
                                           {'get': True})
        idmap_id = idmap.pop('id')
        if not idmap['range_low']:
            idmap['range_low'], idmap['range_high'] = await self.middleware.call('idmap.get_next_idmap_range')
        idmap['dns_domain_name'] = our_domain.upper()
        await self.middleware.call('idmap.update', idmap_id, idmap)

    @private
    async def add_privileges(self, domain_name, workgroup):
        """
        Grant Domain Admins full control of server
        """
        existing_privileges = await self.middleware.call(
            'privilege.query',
            [["name", "=", domain_name]]
        )
        if existing_privileges:
            return

        domain_info = await self.middleware.call('idmap.domain_info', workgroup)
        await self.middleware.call('privilege.create', {
            'name': domain_name,
            'ds_groups': [f'{domain_info["sid"]}-512'],
            'allowlist': [{'method': '*', 'resource': '*'}],
            'web_shell': True
        })

    @private
    async def remove_privileges(self, domain_name):
        """
        Remove any auto-granted domain privileges
        """
        existing_privileges = await self.middleware.call(
            'privilege.query',
            [["name", "=", domain_name]]
        )
        if not existing_privileges:
            return

        await self.middleware.call('privilege.delete', existing_privileges[0]['id'])

    @private
    async def post_join_setup(self, job, data):
        ad = data['ad_config']
        smb = data['smb_config']
        smb_ha_mode = data['ha_mode']

        await self.middleware.call('activedirectory.register_dns', ad, smb, smb_ha_mode)

        """
        Manipulating the SPN entries must be done with elevated privileges. Add NFS service
        principals while we have these on-hand.
        Since this may potentially take more than a minute to complete, run in background job.
        """
        job.set_progress(60, 'Adding NFS Principal entries.')
        # Skip health check for add_nfs_spn since by this point our AD join should be de-facto healthy.
        spn_job = await self.middleware.call('activedirectory.add_nfs_spn', ad['netbiosname'], ad['domainname'], False, False)
        await spn_job.wait()

        job.set_progress(70, 'Storing computer account keytab.')
        await self.middleware.call('kerberos.keytab.store_ad_keytab')

    async def __start(self, job):
        """
        Start AD service. In 'UNIFIED' HA configuration, only start AD service
        on active storage controller.
        """
        ad = await self.config()
        smb = await self.middleware.call('smb.config')
        workgroup = smb['workgroup']
        smb_ha_mode = await self.middleware.call('smb.reset_smb_ha_mode')
        if smb_ha_mode == 'UNIFIED':
            if await self.middleware.call('failover.status') != 'MASTER':
                return

        state = await self.get_state()
        if state in [DSStatus['JOINING'], DSStatus['LEAVING']]:
            raise CallError(f'Active Directory Service has status of [{state}]. Wait until operation completes.', errno.EBUSY)

        dc_info = await self.lookup_dc(ad['domainname'])

        await self.set_state(DSStatus['JOINING'].name)
        job.set_progress(0, 'Preparing to join Active Directory')
        if ad['verbose_logging']:
            self.logger.debug('Starting Active Directory service for [%s]', ad['domainname'])

        await self.middleware.call('datastore.update', self._config.datastore, ad['id'], {'enable': True}, {'prefix': 'ad_'})
        await self.middleware.call('etc.generate', 'smb')
        await self.middleware.call('etc.generate', 'hostname')

        """
        Kerberos realm field must be populated so that we can perform a kinit
        and use the kerberos ticket to execute 'net ads' commands.
        """
        job.set_progress(5, 'Configuring Kerberos Settings.')
        if not ad['kerberos_realm']:
            try:
                realm_id = (await self.middleware.call(
                    'kerberos.realm.query',
                    [('realm', '=', ad['domainname'])],
                    {'get': True}
                ))['id']
            except MatchNotFound:
                realm_id = await self.middleware.call(
                    'datastore.insert', 'directoryservice.kerberosrealm',
                    {'krb_realm': ad['domainname'].upper()}
                )

            await self.middleware.call(
                'datastore.update', self._config.datastore, ad['id'],
                {"kerberos_realm": realm_id}, {'prefix': 'ad_'}
            )
            ad = await self.config()

        if not await self.middleware.call(
            'kerberos.check_ticket',
            {'ccache': krb5ccache.SYSTEM.name},
            False
        ):
            await self.middleware.call('kerberos.start')

        """
        'workgroup' is the 'pre-Windows 2000 domain name'. It must be set to the nETBIOSName value in Active Directory.
        This must be properly configured in order for Samba to work correctly as an AD member server.
        'site' is the ad site of which the NAS is a member. If sites and subnets are unconfigured this will
        default to 'Default-First-Site-Name'.
        """

        job.set_progress(20, 'Detecting Active Directory Site.')
        if not ad['site']:
            ad['site'] = dc_info['Client Site Name']
            if dc_info['Client Site Name'] != 'Default-First-Site-Name':
                await self.middleware.call('activedirectory.set_kerberos_servers', ad)

        job.set_progress(30, 'Detecting Active Directory NetBIOS Domain Name.')
        if workgroup != dc_info['Pre-Win2k Domain']:
            self.logger.debug('Updating SMB workgroup to %s', dc_info['Pre-Win2k Domain'])
            await self.middleware.call('datastore.update', 'services.cifs', smb['id'], {
                'cifs_srv_workgroup': dc_info['Pre-Win2k Domain']
            })
            workgroup = dc_info['Pre-Win2k Domain']

        # Ensure smb4.conf has correct workgorup.
        await self.middleware.call('etc.generate', 'smb')

        """
        Check response of 'net ads testjoin' to determine whether the server needs to be joined to Active Directory.
        Only perform the domain join if we receive the exact error code indicating that the server is not joined to
        Active Directory. 'testjoin' will fail if the NAS boots before the domain controllers in the environment.
        In this case, samba should be started, but the directory service reported in a FAULTED state.
        """

        job.set_progress(40, 'Performing testjoin to Active Directory Domain')
        machine_acct = f'{ad["netbiosname"].upper()}$@{ad["domainname"]}'
        ret = await self._net_ads_testjoin(workgroup, ad)
        if ret == neterr.NOTJOINED:
            job.set_progress(50, 'Joining Active Directory Domain')
            self.logger.debug(f"Test join to {ad['domainname']} failed. Performing domain join.")
            await self._net_ads_join(workgroup, ad)

            try:
                await self.post_join_setup(job, {
                    'ad_config': ad,
                    'smb_config': smb,
                    'ha_mode': smb_ha_mode
                })
            except Exception:
                self.logger.error("Tasks subsequent to Active Directory join failed. "
                                  "Attempting to roll-back join attempt.", exc_info=True)
                await self._net_ads_leave({'username': ad['bindname']})
                raise

            await self.middleware.call('datastore.update', self._config.datastore, ad['id'], {
                'kerberos_principal': machine_acct
            }, {'prefix': 'ad_'})

            ad = await self.config()

            job.set_progress(75, 'Performing kinit using new computer account.')

            """
            Remove our temporary administrative ticket and replace with machine account.

            Sysvol replication may not have completed (new account only exists on the DC we're
            talking to) and so during this operation we need to hard-code which KDC we use for
            the new kinit.
            """
            domain_info = await self.domain_info(ad['domainname'])
            cred = await self.middleware.call('kerberos.get_cred', {
                'dstype': DSType.DS_TYPE_ACTIVEDIRECTORY.name,
                'conf': {
                    'domainname': ad['domainname'],
                    'kerberos_principal': machine_acct,
                }
            })
            # remove admin ticket
            await self.middleware.call('kerberos.kdestroy')

            # remove stub krb5.conf to allow overriding with fix on KDC
            await self.middleware.run_in_thread(os.unlink, '/etc/krb5.conf')
            await self.middleware.call('kerberos.do_kinit', {
                'krb5_cred': cred,
                'kinit-options': {
                    'kdc_override': {'domain': ad['domainname'], 'kdc': domain_info['KDC server']}
                }
            })
            await self.middleware.call('kerberos.wait_for_renewal')
            await self.middleware.call('etc.generate', 'kerberos')

            job.set_progress(80, 'Configuring idmap backend and NTP servers.')
            await self.middleware.call('service.update', 'cifs', {'enable': True})
            await self.set_idmap(ad['allow_trusted_doms'], ad['domainname'])
            await self.middleware.call('activedirectory.set_ntp_servers')
            await self.middleware.call("directoryservices.secrets.backup")
            ret = neterr.JOINED
        elif ret == neterr.JOINED:
            # We are already joined to AD. User may have disabled then re-renabled the plugin
            # Check whether we have valid kerberos principal
            if not ad['kerberos_principal']:
                if not await self.middleware.call('kerberos.keytab.query', [['name', '=', 'AD_MACHINE_ACCOUNT']]):
                    # Force writing of keytab based on stored secrets to our config file.
                    await self.middleware.call('activedirectory.check_machine_account_keytab', ad['domainname'])

                await self.middleware.call('datastore.update', self._config.datastore, ad['id'], {
                    'kerberos_principal': machine_acct
                }, {'prefix': 'ad_'})

        await self.middleware.call('etc.generate', 'smb')
        await self.middleware.call('service.restart', 'idmap')
        await self.middleware.call('etc.generate', 'pam')
        await self.middleware.call('etc.generate', 'nss')
        if ret == neterr.JOINED:
            await self.set_state(DSStatus['HEALTHY'].name)
            job.set_progress(90, 'Restarting dependent services.')
            cache_fill = await self.middleware.call('directoryservices.cache.refresh_impl')
            await cache_fill.wait()
            await self.middleware.call('directoryservices.restart_dependent_services')
            if ad['verbose_logging']:
                self.logger.debug('Successfully started AD service for [%s].', ad['domainname'])

        else:
            await self.set_state(DSStatus['FAULTED'].name)
            self.logger.warning('Server is joined to domain [%s], but is in a faulted state.', ad['domainname'])

        job.set_progress(100, f'Active Directory start completed with status [{ret.name}]')
        await self.middleware.call('service.reload', 'idmap')

        if ret == neterr.JOINED:
            job.set_progress(100, 'Granting privileges to domain admins.')
            try:
                await self.add_privileges(ad['domainname'], dc_info['Pre-Win2k Domain'])
            except Exception:
                self.logger.warning('Failed to grant Domain Admins privileges', exc_info=True)

        return ret.name

    async def __stop(self, job, config):
        job.set_progress(0, 'Preparing to stop Active Directory service')
        await self.middleware.call(
            'datastore.update', self._config.datastore,
            config['id'], {'ad_enable': False}
        )

        await self.set_state(DSStatus['LEAVING'].name)
        job.set_progress(5, 'Stopping Active Directory monitor')
        await self.middleware.call('etc.generate', 'hostname')
        job.set_progress(10, 'Stopping kerberos service')
        await self.middleware.call('kerberos.stop')
        job.set_progress(20, 'Reconfiguring SMB.')
        await self.middleware.call('service.stop', 'cifs')
        await self.middleware.call('service.restart', 'idmap')
        job.set_progress(40, 'Reconfiguring pam and nss.')
        await self.middleware.call('etc.generate', 'pam')
        await self.middleware.call('etc.generate', 'nss')
        await self.set_state(DSStatus['DISABLED'].name)
        job.set_progress(60, 'clearing caches.')
        await self.middleware.call('directoryservices.cache.abort_refresh')
        await self.middleware.call('service.start', 'cifs')
        await self.set_state(DSStatus['DISABLED'].name)
        job.set_progress(100, 'Active Directory stop completed.')

    @private
    async def validate_credentials(self, ad=None, kdc=None):
        """
        Kinit with user-provided credentials is sufficient to determine
        whether the credentials are good. A testbind here is unnecessary.
        """
        if await self.middleware.call(
            'kerberos.check_ticket',
            {'ccache': krb5ccache.SYSTEM.name},
            False
        ):
            # Short-circuit credential validation if we have a valid tgt
            return

        ad = ad or await self.config()
        payload = {
            'dstype': DSType.DS_TYPE_ACTIVEDIRECTORY.name,
            'conf': {
                'bindname': ad.get('bindname', ''),
                'bindpw': ad.get('bindpw', ''),
                'domainname': ad['domainname'],
                'kerberos_principal': ad['kerberos_principal'],
            }
        }
        cred = await self.middleware.call('kerberos.get_cred', payload)
        await self.middleware.call('kerberos.do_kinit', {
            'krb5_cred': cred,
            'kinit-options': {'kdc_override': {'domain': ad['domainname'], 'kdc': kdc}},
        })
        return

    @private
    async def _parse_join_err(self, msg):
        if len(msg) < 2:
            raise CallError(msg)

        if "Invalid configuration" in msg[1]:
            """
            ./source3/libnet/libnet_join.c will return configuration erros for the
            following situations:
            - incorrect workgroup
            - incorrect realm
            - incorrect security settings
            Unless users set auxiliary parameters, only the first should be a possibility.
            """
            raise CallError(f'{msg[1].rsplit(")",1)[0]}).', errno.EINVAL)
        else:
            raise CallError(msg[1])

    @private
    async def _net_ads_join(self, workgroup, ad):
        await self.middleware.call("kerberos.check_ticket")
        cmd = [
            SMBCmd.NET.value,
            '--use-kerberos', 'required',
            '--use-krb5-ccache', krb5ccache.SYSTEM.value,
            '-w', workgroup,
            '-U', ad['bindname'],
            '-d', '5',
            'ads', 'join'
        ]

        if ad['createcomputer']:
            cmd.append(f'createcomputer={ad["createcomputer"]}')

        cmd.extend(['--no-dns-updates', ad['domainname']])
        netads = await run(cmd, check=False)
        if netads.returncode != 0:
            self.logger.warning("AD JOIN FAILED: %s", netads.stderr.decode())
            await self.set_state(DSStatus['FAULTED'].name)
            await self._parse_join_err(netads.stdout.decode().split(':', 1))

    @private
    async def _net_ads_testjoin(self, workgroup, ad=None):
        """
        If neterr.NOTJOINED is returned then we will proceed with joining (or re-joining)
        the AD domain. There are currently two reasons to do this:
        1) we're not joined to AD
        2) our computer account was deleted out from under us
        It's generally better to report an error condition to the end user and let them
        fix it, but situation (2) above is straightforward enough to automatically re-join.
        In this case, the error message presents oddly because stale credentials are stored in
        the secrets.tdb file and the message is passed up from underlying KRB5 library.
        """
        await self.middleware.call("kerberos.check_ticket")
        if ad is None:
            ad = await self.config()

        cmd = [
            SMBCmd.NET.value,
            '--use-kerberos', 'required',
            '--use-krb5-ccache', krb5ccache.SYSTEM.value,
            '-w', workgroup,
            '-d', '5',
            'ads', 'testjoin'
        ]

        netads = await run(cmd, check=False)
        if netads.returncode != 0:
            errout = netads.stderr.decode()
            with open(f"{SMBPath.LOGDIR.platform()}/domain_testjoin_{int(datetime.datetime.now().timestamp())}.log", "w") as f:
                f.write(errout)

            return neterr.to_status(errout)

        return neterr.JOINED

    @private
    async def _net_ads_leave(self, data):
        await self.middleware.call('kerberos.check_ticket')

        cmd = [
            SMBCmd.NET.value,
            '--use-kerberos', 'required',
            '--use-krb5-ccache', krb5ccache.SYSTEM.value,
            '-U', data['username'],
            'ads', 'leave',
        ]

        netads = await run(cmd, check=False)
        if netads.returncode != 0:
            self.logger.warning("Failed to leave domain: %s", netads.stderr.decode())
            return False

        return True

    @accepts(Str('domain', default=''), roles=['DIRECTORY_SERVICE_READ'])
    @returns(Dict(
        IPAddr('LDAP server'),
        Str('LDAP server name'),
        Str('Realm'),
        LDAP_DN('Bind Path'),
        Int('LDAP port'),
        Int('Server time'),
        IPAddr('KDC server'),
        Int('Server time offset'),
        Int('Last machine account password change')
    ))
    async def domain_info(self, domain):
        """
        Returns the following information about the currently joined domain:

        `LDAP server` IP address of current LDAP server to which TrueNAS is connected.

        `LDAP server name` DNS name of LDAP server to which TrueNAS is connected

        `Realm` Kerberos realm

        `LDAP port`

        `Server time` timestamp.

        `KDC server` Kerberos KDC to which TrueNAS is connected

        `Server time offset` current time offset from DC.

        `Last machine account password change`. timestamp
        """
        if domain:
            cmd = [SMBCmd.NET.value, '-S', domain, '--json', '--option', f'realm={domain}', 'ads', 'info']
        else:
            cmd = [SMBCmd.NET.value, '--json', 'ads', 'info']

        netads = await self.cache_flush_retry(cmd)
        if netads.returncode != 0:
            err_msg = netads.stderr.decode().strip()
            if err_msg == "Didn't find the ldap server!":
                raise CallError(
                    'Failed to discover Active Directory Domain Controller '
                    'for domain. This may indicate a DNS misconfiguration.',
                    errno.ENOENT
                )

            raise CallError(netads.stderr.decode())

        return json.loads(netads.stdout.decode())

    @private
    async def set_ntp_servers(self):
        """
        Appropriate time sources are a requirement for an AD environment. By default kerberos authentication
        fails if there is more than a 5 minute time difference between the AD domain and the member server.
        """
        ntp_servers = await self.middleware.call('system.ntpserver.query')
        ntp_pool = 'debian.pool.ntp.org'
        default_ntp_servers = list(filter(lambda x: ntp_pool in x['address'], ntp_servers))
        if len(ntp_servers) != 3 or len(default_ntp_servers) != 3:
            return

        try:
            dc_info = await self.lookup_dc()
        except CallError:
            self.logger.warning("Failed to automatically set time source.", exc_info=True)
            return

        if not dc_info['Flags']['Is running time services']:
            return

        dc_name = dc_info["Information for Domain Controller"]

        try:
            await self.middleware.call('system.ntpserver.create', {'address': dc_name, 'prefer': True})
        except Exception:
            self.logger.warning('Failed to configure NTP for the Active Directory domain. Additional '
                                'manual configuration may be required to ensure consistent time offset, '
                                'which is required for a stable domain join.', exc_info=True)
        return

    @private
    async def cache_flush_retry(self, cmd, retry=True):
        rv = await run(cmd, check=False)
        if rv.returncode != 0 and retry:
            await self.middleware.call('idmap.gencache.flush')
            return await self.cache_flush_retry(cmd, False)

        return rv

    @private
    async def lookup_dc(self, domain=None):
        if domain is None:
            domain = (await self.config())['domainname']

        lookup = await self.cache_flush_retry([SMBCmd.NET.value, '--json', '-S', domain, '--realm', domain, 'ads', 'lookup'])
        if lookup.returncode != 0:
            raise CallError("Failed to look up Domain Controller information: "
                            f"{lookup.stderr.decode().strip()}")

        out = json.loads(lookup.stdout.decode())
        return out

    @accepts(Ref('kerberos_username_password'), roles=['DIRECTORY_SERVICE_WRITE'])
    @returns()
    @job(lock="AD_start_stop")
    async def leave(self, job, data):
        """
        Leave Active Directory domain. This will remove computer
        object from AD and clear relevant configuration data from
        the NAS.
        This requires credentials for appropriately-privileged user.
        Credentials are used to obtain a kerberos ticket, which is
        used to perform the actual removal from the domain.
        """
        ad = await self.config()
        if not ad['domainname']:
            raise CallError('Active Directory domain name present in configuration.')

        ad['bindname'] = data.get("username", "")
        ad['bindpw'] = data.get("password", "")
        ad['kerberos_principal'] = ''

        payload = {
            'dstype': DSType.DS_TYPE_ACTIVEDIRECTORY.name,
            'conf': {
                'bindname': data.get('username', ''),
                'bindpw': data.get('password', ''),
                'domainname': ad['domainname'],
                'kerberos_principal': '',
            }
        }

        try:
            await self.remove_privileges(ad['domainname'])
        except Exception:
            self.logger.warning('Failed to remove Domain Admins privileges', exc_info=True)

        job.set_progress(5, 'Obtaining kerberos ticket for privileged user.')
        cred = await self.middleware.call('kerberos.get_cred', payload)
        await self.middleware.call('kerberos.do_kinit', {'krb5_cred': cred})

        job.set_progress(10, 'Leaving Active Directory domain.')
        left_successfully = await self._net_ads_leave(data)

        job.set_progress(15, 'Removing DNS entries')
        await self.middleware.call('activedirectory.unregister_dns', ad)

        job.set_progress(20, 'Removing kerberos keytab and realm.')
        krb_princ = await self.middleware.call(
            'kerberos.keytab.query',
            [('name', '=', 'AD_MACHINE_ACCOUNT')]
        )
        if krb_princ:
            await self.middleware.call(
                'datastore.delete', 'directoryservice.kerberoskeytab', krb_princ[0]['id']
            )

        if ad['kerberos_realm']:
            try:
                await self.middleware.call(
                    'datastore.delete', 'directoryservice.kerberosrealm', ad['kerberos_realm']
                )
            except MatchNotFound:
                pass

        if left_successfully:
            try:
                await self.middleware.call("directoryservices.secrets.backup")
            except Exception:
                self.logger.debug("Failed to remove stale secrets entries.", exc_info=True)

        job.set_progress(30, 'Clearing local Active Directory settings.')
        payload = {
            'enable': False,
            'site': None,
            'bindname': '',
            'kerberos_realm': None,
            'kerberos_principal': '',
            'domainname': '',
        }
        await self.middleware.call(
            'datastore.update', self._config.datastore,
            ad['id'], payload, {'prefix': 'ad_'}
        )
        await self.set_state(DSStatus['DISABLED'].name)

        job.set_progress(40, 'Flushing caches.')
        try:
            await self.middleware.call('idmap.gencache.flush')
        except Exception:
            self.logger.warning("Failed to flush cache after leaving Active Directory.", exc_info=True)

        with contextlib.suppress(FileNotFoundError):
            os.unlink('/etc/krb5.keytab')

        job.set_progress(50, 'Clearing kerberos configuration and ticket.')
        await self.middleware.call('kerberos.stop')

        job.set_progress(60, 'Regenerating configuration.')
        await self.middleware.call('etc.generate', 'pam')
        await self.middleware.call('etc.generate', 'nss')
        await self.middleware.call('etc.generate', 'smb')

        job.set_progress(60, 'Restarting services.')
        await self.middleware.call('service.restart', 'cifs')
        await self.middleware.call('service.restart', 'idmap')
        job.set_progress(100, 'Successfully left activedirectory domain.')
        return
