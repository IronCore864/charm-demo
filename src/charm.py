#!/usr/bin/env python3
# This file needs to be executable. Shebang to ensure that the file is directly executable.
# Run: `chmod a+x src/charm.py``

import json
import logging
import requests
from typing import Any

from charms.data_platform_libs.v0.data_interfaces import DatabaseCreatedEvent
from charms.data_platform_libs.v0.data_interfaces import DatabaseRequires
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus
from ops.model import BlockedStatus
from ops.model import MaintenanceStatus
from ops.model import WaitingStatus
from ops.pebble import Layer

# match the name of the peer relation defined in metadata.yaml file
PEER_NAME = "fastapi-peer"

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)


class FastAPIDemoCharm(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)

        self.pebble_service_name = "fastapi-service"
        self.container = self.unit.get_container(
            "demo-server"
        )  # see 'containers' in metadata.yaml

        # add an observer for when the Juju controller informs the charm that the Pebble in its workload container is up and running
        # the observer is a function that takes as an argument an event and an event handler
        # The event name is created automatically by Ops for each container on the template <container name>-pebble-ready
        # The event handler is a method in your charm class that will be executed when the event is fired
        # in this case, you will use it to tell Pebble how to start your application.
        self.framework.observe(
            self.on.demo_server_pebble_ready, self._update_layer_and_restart
        )

        # configuration options
        self.framework.observe(self.on.config_changed, self._on_config_changed)

        # relation_name is from metadata.yaml
        # database_name is the name of the database that the app requires
        self.database = DatabaseRequires(
            self, relation_name="database", database_name="names_db"
        )
        # docs: https://charmhub.io/data-platform-libs/libraries/data_interfaces
        self.framework.observe(
            self.database.on.database_created, self._on_database_created
        )
        self.framework.observe(
            self.database.on.endpoints_changed, self._on_database_created
        )
        # This will handle the situation where the charm user decides to remove the relation to the database
        self.framework.observe(
            self.on.database_relation_broken, self._on_database_relation_removed
        )

        self.framework.observe(self.on.start, self._count)

        # events on custom actions that are run via 'juju run-action'
        self.framework.observe(self.on.get_db_info_action, self._on_get_db_info_action)

    def _update_layer_and_restart(self, event) -> None:
        """Define and start a workload using the Pebble API.
        more about Pebble layers at https://github.com/canonical/pebble
        """

        # more on status: https://juju.is/docs/sdk/constructs#heading--statuses
        self.unit.status = MaintenanceStatus("Assembling pod spec")

        if self.container.can_connect():
            new_layer = self._pebble_layer.to_dict()
            # get the current pebble layer config from the container
            services = self.container.get_plan().to_dict().get("services", {})
            if services != new_layer["services"]:
                # Changes were made, add the new layer
                self.container.add_layer(
                    "fastapi_demo", self._pebble_layer, combine=True
                )
                logger.info("Added updated layer 'fastapi_demo' to Pebble plan")

                self.container.restart(self.pebble_service_name)
                logger.info(f"Restarted '{self.pebble_service_name}' service")

            # add workload version in juju status
            self.unit.set_workload_version(self.version)
            self.unit.status = ActiveStatus()
        else:
            self.unit.status = WaitingStatus("Waiting for Pebble in workload container")

    @property
    def _pebble_layer(self):
        """Return a dictionary representing a Pebble layer."""
        command = " ".join(
            [
                "uvicorn",
                "api_demo_server.app:app",
                "--host=0.0.0.0",
                f"--port={self.config['server-port']}",
            ]
        )
        pebble_layer = {
            "summary": "FastAPI demo service",
            "description": "pebble config layer for FastAPI demo server",
            "services": {
                self.pebble_service_name: {
                    "override": "replace",
                    "summary": "fastapi demo",
                    "command": command,
                    "startup": "enabled",
                    "environment": self.app_environment,
                }
            },
        }
        return Layer(pebble_layer)

    def _on_config_changed(self, event):
        port = self.config["server-port"]  # see config.yaml
        if int(port) == 22:
            self.unit.status = BlockedStatus(
                "Invalid port number, 22 is reserved for SSH"
            )
            return
        self._handle_ports()
        logger.debug("New application port is requested: %s", port)
        self._update_layer_and_restart(None)

    def _handle_ports(self):
        port = int(self.config["server-port"])
        # k8s service port
        self.unit.set_ports(port)

    @property
    def version(self) -> str:
        if self.container.can_connect() and self.container.get_services(
            self.pebble_service_name
        ):
            try:
                return self._request_version()
            except Exception as e:
                logger.warning("unable to get version from API: %s", str(e))
                logger.exception(e)
        return ""

    def _request_version(self) -> str:
        return requests.get(
            f"http://localhost:{self.config['server-port']}/version", timeout=10
        ).json()["version"]

    def _on_database_created(self, event: DatabaseCreatedEvent) -> None:
        self._update_layer_and_restart(None)

    def _on_database_relation_removed(self, event) -> None:
        self.unit.status = WaitingStatus("Waiting for database relation")
        raise SystemExit(0)

    @property
    def app_environment(self):
        """create a dict of env vars for the app"""
        db_data = self.fetch_postgres_relation_data()
        env = {
            "DEMO_SERVER_DB_HOST": db_data.get("db_host", None),
            "DEMO_SERVER_DB_PORT": db_data.get("db_port", None),
            "DEMO_SERVER_DB_USER": db_data.get("db_username", None),
            "DEMO_SERVER_DB_PASSWORD": db_data.get("db_password", None),
        }
        return env

    def fetch_postgres_relation_data(self) -> dict:
        relations = self.database.fetch_relation_data()
        logger.debug("Got following database data: %s", relations)
        for data in relations.values():
            if not data:
                continue
            logger.info("New PSQL database endpoint is %s", data["endpoints"])
            host, port = data["endpoints"].split(":")
            db_data = {
                "db_host": host,
                "db_port": port,
                "db_username": data["username"],
                "db_password": data["password"],
            }
            return db_data
        self.unit.status = WaitingStatus("Waiting for database relation")
        raise SystemExit(0)

    @property
    def peers(self):
        return self.model.get_relation(PEER_NAME)

    def set_peer_data(self, key: str, data: Any) -> None:
        self.peers.data[self.app][key] = json.dumps(data)

    def get_peer_data(self, key: str) -> dict[Any, Any]:
        if not self.peers:
            return {}
        data = self.peers.data[self.app].get(key, "")
        return json.loads(data) if data else {}

    def _count(self, event) -> None:
        unit_stats = self.get_peer_data("unit_stats")
        counter = unit_stats.get("started_counter", 0)
        self.set_peer_data("unit_stats", {"started_counter": int(counter) + 1})

    def _on_get_db_info_action(self, event) -> None:
        """name matches what's defined in actions.yaml"""

        # matches the parameter name defined in actions.yaml
        show_password = event.params["show-password"]
        try:
            db_data = self.fetch_postgres_relation_data()
            output = {
                "db-host": db_data.get("db_host", None),
                "db-port": db_data.get("db_port", None),
            }

            if show_password:
                output.update(
                    {
                        "db-username": db_data.get("db_username", None),
                        "db-password": db_data.get("db_password", None),
                    }
                )
        except SystemExit:
            output = {"result": "No database connected"}
            raise
        finally:
            event.set_results(output)


if __name__ == "__main__":
    main(FastAPIDemoCharm)
