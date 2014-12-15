# All Rights Reserved.
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

from rally.benchmark.context import base
from rally.benchmark.context.cleanup import manager as resource_manager
from rally.benchmark.scenarios.cinder import utils as cinder_utils
from rally.i18n import _
from rally import log as logging
from rally import osclients
from rally import utils as rutils


LOG = logging.getLogger(__name__)


@base.context(name="volumes", order=420)
class VolumeGenerator(base.Context):
    """Context class for adding volumes to each user for benchmarks."""

    CONFIG_SCHEMA = {
        "type": "object",
        "$schema": rutils.JSON_SCHEMA,
        "properties": {
            "size": {
                "type": "integer",
                "minimum": 1
            },
        },
        'required': ['size'],
        "additionalProperties": False
    }

    def __init__(self, context):
        super(VolumeGenerator, self).__init__(context)

    @rutils.log_task_wrapper(LOG.info, _("Enter context: `Volumes`"))
    def setup(self):
        size = self.config["size"]

        for user, tenant_id in rutils.iterate_per_tenants(
                self.context["users"]):
            clients = osclients.Clients(user["endpoint"])
            cinder_util = cinder_utils.CinderScenario(clients=clients)
            volume = cinder_util._create_volume(size)

            self.context["tenants"][tenant_id]["volume"] = volume.id

    @rutils.log_task_wrapper(LOG.info, _("Exit context: `Volumes`"))
    def cleanup(self):
        # TODO(boris-42): Delete only resources created by this context
        resource_manager.cleanup(names=["cinder.volumes"],
                                 users=self.context.get("users", []))
