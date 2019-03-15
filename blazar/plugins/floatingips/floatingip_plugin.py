# Copyright (c) 2019 NTT.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import datetime

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import netutils
from oslo_utils import strutils

from blazar import context
from blazar.db import api as db_api
from blazar.db import exceptions as db_ex
from blazar.db import utils as db_utils
from blazar import exceptions
from blazar.manager import exceptions as manager_ex
from blazar.plugins import base
from blazar.plugins import floatingips as plugin
from blazar import status
from blazar.utils.openstack import neutron
from blazar.utils import plugins as plugins_utils


CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class FloatingIpPlugin(base.BasePlugin):
    """Plugin for floating IP resource."""

    resource_type = plugin.RESOURCE_TYPE
    title = 'Floating IP Plugin'
    description = 'This plugin creates and assigns floating IPs.'

    def check_params(self, values):
        if 'network_id' not in values:
            raise manager_ex.MissingParameter(param='network_id')

        if 'amount' not in values:
            raise manager_ex.MissingParameter(param='amount')

        if not strutils.is_int_like(values['amount']):
            raise manager_ex.MalformedParameter(param='amount')

        # floating_ip_address param is an optional parameter
        fips = values.get('required_floatingips', [])
        if not isinstance(fips, list):
            manager_ex.MalformedParameter(param='required_floatingips')

        for ip in fips:
            if not (netutils.is_valid_ipv4(ip) or netutils.is_valid_ipv6(ip)):
                raise manager_ex.InvalidIPFormat(ip=ip)

    def reserve_resource(self, reservation_id, values):
        """Create floating IP reservation."""
        self.check_params(values)

        required_fips = values.get('required_floatingips', [])
        amount = int(values['amount'])

        if len(required_fips) > amount:
            raise manager_ex.TooLongFloatingIPs()

        floatingip_ids = self._matching_fips(values['network_id'],
                                             required_fips,
                                             amount,
                                             values['start_date'],
                                             values['end_date'])

        floatingip_rsrv_values = {
            'reservation_id': reservation_id,
            'network_id': values['network_id'],
            'amount': amount
        }

        fip_reservation = db_api.fip_reservation_create(floatingip_rsrv_values)
        for fip_address in required_fips:
            fip_address_values = {
                'address': fip_address,
                'floatingip_reservation_id': fip_reservation['id']
            }
            db_api.required_fip_create(fip_address_values)
        for fip_id in floatingip_ids:
            db_api.fip_allocation_create({'floatingip_id': fip_id,
                                          'reservation_id': reservation_id})
        return fip_reservation['id']

    def update_reservation(self, reservation_id, values):
        reservation = db_api.reservation_get(reservation_id)
        lease = db_api.lease_get(reservation['lease_id'])

        updatable = ['network_id', 'floating_ip_address']
        if (not any([k in updatable for k in values.keys()])
                and values['start_date'] >= lease['start_date']
                and values['end_date'] <= lease['end_date']):
            # no update because of just shortening the reservation time
            return

        dates_before = {'start_date': lease['start_date'],
                        'end_date': lease['end_date']}
        dates_after = {'start_date': values['start_date'],
                       'end_date': values['end_date']}

        fip_reservation = db_api.fip_reservation_get(
            reservation['resource_id'])
        self._update_allocations(dates_before, dates_after, reservation_id,
                                 reservation['status'], fip_reservation,
                                 values)

        updates = {key: values[key] for key in updatable if key in values}
        if updates:
            db_api.fip_reservation_update(fip_reservation['id'], updates)

    def _update_allocations(self, dates_before, dates_after, reservation_id,
                            reservation_status, fip_reservation, values):
        network_id = values.get('network_id', fip_reservation['network_id'])
        fip_address = values.get('floating_ip_address',
                                 fip_reservation['floating_ip_address'])

        alloc_to_remove = self._allocation_to_remove(
            dates_before, dates_after, reservation_id, network_id, fip_address)

        # Nothing to update or extending current allocation to new time window.
        if not alloc_to_remove:
            return

        if (alloc_to_remove and
           reservation_status == status.reservation.ACTIVE):
            raise manager_ex.NotEnoughFloatingIPAvailable()

        floatingip_ids = self._matching_fips(network_id,
                                             fip_address,
                                             dates_after['start_date'],
                                             dates_before['end_date'])
        if not floatingip_ids:
            raise manager_ex.NotEnoughFloatingIPAvailable()

        db_api.fip_allocation_create({'floatingip_id': floatingip_ids[0],
                                      'reservation_id': reservation_id})
        db_api.fip_allocation_destroy(alloc_to_remove['id'])

    def _allocation_to_remove(self, dates_before, dates_after, reservation_id,
                              network_id, fip_address):
        alloc = next(iter(db_api.fip_allocation_get_all_by_values(
            reservation_id=reservation_id)))
        fip_ids = [fip['id'] for fip in
                   self._filter_fips_by_properties(network_id, fip_address)]

        # The update_reservation tries to reserve the same floating IP
        # if the floating IP is available during the new time window.
        if alloc['floatingip_id'] in fip_ids:
            reserved_periods = db_utils.get_reserved_periods(
                alloc['floatingip_id'],
                dates_after['start_date'],
                dates_after['end_date'],
                datetime.timedelta(seconds=1),
                resource_type='floatingip')

            max_start = max(dates_before['start_date'],
                            dates_after['start_date'])
            min_end = min(dates_before['end_date'],
                          dates_after['end_date'])

            if (len(reserved_periods) == 0 or
                (len(reserved_periods) == 1 and
                 reserved_periods[0][0] == max_start and
                 reserved_periods[0][1] == min_end)):
                return []
        return alloc

    def _filter_fips_by_properties(self, network_id, fip_address):
        query_filter = plugins_utils.convert_requirements(
            ['==', '$network_id', network_id])
        if fip_address:
            query = ["==", '$floating_ip_address', fip_address]
            query_filter += plugins_utils.convert_requirements(query)
        return db_api.reservable_fip_get_all_by_queries(query_filter)

    def on_start(self, resource_id):
        fip_reservation = db_api.fip_reservation_get(resource_id)
        allocations = db_api.fip_allocation_get_all_by_values(
            reservation_id=fip_reservation['reservation_id'])

        ctx = context.current()
        fip_pool = neutron.FloatingIPPool(fip_reservation['network_id'])
        for alloc in allocations:
            fip = db_api.floatingip_get(alloc['floatingip_id'])
            fip_pool.create_reserved_floatingip(
                fip['subnet_id'], fip['floating_ip_address'],
                ctx.project_id, fip_reservation['reservation_id'])

    def on_end(self, resource_id):
        fip_reservation = db_api.fip_reservation_get(resource_id)
        allocations = db_api.fip_allocation_get_all_by_values(
            reservation_id=fip_reservation['reservation_id'])

        fip_pool = neutron.FloatingIPPool(fip_reservation['network_id'])
        for alloc in allocations:
            fip = db_api.floatingip_get(alloc['floatingip_id'])
            fip_pool.delete_reserved_floatingip(fip['floating_ip_address'])

    def _matching_fips(self, network_id, fip_addresses, amount,
                       start_date, end_date):
        filter_array = []
        start_date_with_margin = start_date - datetime.timedelta(
            minutes=CONF.cleaning_time)
        end_date_with_margin = end_date + datetime.timedelta(
            minutes=CONF.cleaning_time)

        fip_query = ["==", "$floating_network_id", network_id]
        filter_array = plugins_utils.convert_requirements(fip_query)

        fip_ids = []
        not_allocated_fip_ids = []
        allocated_fip_ids = []
        for fip in db_api.reservable_fip_get_all_by_queries(filter_array):
            if not db_api.fip_allocation_get_all_by_values(
                    floatingip_id=fip['id']):
                if fip['floating_ip_address'] in fip_addresses:
                    fip_ids.append(fip['id'])
                else:
                    not_allocated_fip_ids.append(fip['id'])
            elif db_utils.get_free_periods(
                    fip['id'],
                    start_date_with_margin,
                    end_date_with_margin,
                    end_date_with_margin - start_date_with_margin,
                    resource_type='floatingip'
            ) == [
                (start_date_with_margin, end_date_with_margin),
            ]:
                if fip['floating_ip_address'] in fip_addresses:
                    fip_ids.append(fip['id'])
                else:
                    allocated_fip_ids.append(fip['id'])

        if len(fip_ids) != len(fip_addresses):
            raise manager_ex.NotEnoughFloatingIPAvailable()

        fip_ids += not_allocated_fip_ids
        if len(fip_ids) >= amount:
            return fip_ids[:amount]

        fip_ids += allocated_fip_ids
        if len(fip_ids) >= amount:
            return fip_ids[:amount]

        raise manager_ex.NotEnoughFloatingIPAvailable()

    def validate_floatingip_params(self, values):
        marshall_attributes = set(['floating_network_id',
                                   'floating_ip_address'])
        missing_attr = marshall_attributes - set(values.keys())
        if missing_attr:
            raise manager_ex.MissingParameter(param=','.join(missing_attr))

    def create_floatingip(self, values):

        self.validate_floatingip_params(values)

        network_id = values.pop('floating_network_id')
        floatingip_address = values.pop('floating_ip_address')

        pool = neutron.FloatingIPPool(network_id)
        # validate the floating ip address is out of allocation_pools and
        # within its subnet cidr.
        try:
            subnet = pool.fetch_subnet(floatingip_address)
        except exceptions.BlazarException:
            LOG.info("Floating IP %s in network %s can't be used "
                     "for Blazar's resource.", floatingip_address, network_id)
            raise

        floatingip_values = {
            'floating_network_id': network_id,
            'subnet_id': subnet['id'],
            'floating_ip_address': floatingip_address
        }

        floatingip = db_api.floatingip_create(floatingip_values)

        return floatingip

    def get_floatingip(self, fip_id):
        fip = db_api.floatingip_get(fip_id)
        if fip is None:
            raise manager_ex.FloatingIPNotFound(floatingip=fip_id)
        return fip

    def list_floatingip(self):
        fips = db_api.floatingip_list()
        return fips

    def delete_floatingip(self, fip_id):
        fip = db_api.floatingip_get(fip_id)
        if fip is None:
            raise manager_ex.FloatingIPNotFound(floatingip=fip_id)

        # TODO(masahito): Check no allocation exists for the floating ip here
        # once this plugin supports reserve_resource method.

        try:
            db_api.floatingip_destroy(fip_id)
        except db_ex.BlazarDBException as e:
            raise manager_ex.CantDeleteFloatingIP(floatingip=fip_id,
                                                  msg=str(e))
