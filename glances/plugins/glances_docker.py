# -*- coding: utf-8 -*-
#
# This file is part of Glances.
#
# Copyright (C) 2015 Nicolargo <nicolas@nicolargo.com>
#
# Glances is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Glances is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""Docker plugin."""

import os
import re
import threading
import time

# Import Glances libs
from glances.core.glances_logging import logger
from glances.core.glances_timer import getTimeSinceLastUpdate
from glances.plugins.glances_plugin import GlancesPlugin
from glances.core.glances_timer import Timer

# Docker-py library (optional and Linux-only)
# https://github.com/docker/docker-py
try:
    import docker
    import requests
except ImportError as e:
    logger.debug("Docker library not found (%s). Glances cannot grab Docker info." % e)
    docker_tag = False
else:
    docker_tag = True


class Plugin(GlancesPlugin):

    """Glances Docker plugin.

    stats is a list
    """

    def __init__(self, args=None):
        """Init the plugin."""
        GlancesPlugin.__init__(self, args=args)

        # The plgin can be disable using: args.disable_docker
        self.args = args

        # We want to display the stat in the curse interface
        self.display_curse = True

        # Init the Docker API
        self.docker_client = False

        # Dict of thread (to grab stats asynchroniously, one thread is created by container)
        # key: Container Id
        # value: instance of ThreadDockerGrabber
        self.thread_list = {}

    def exit(self):
        """Overwrite the exit method to close threads"""
        logger.debug("Stop the Docker plugin")
        for t in self.thread_list.values():
            t.stop()

    def get_key(self):
        """Return the key of the list."""
        return 'name'

    def get_export(self):
        """Overwrite the default export method.

        - Only exports containers
        - The key is the first container name
        """
        ret = []
        try:
            ret = self.stats['containers']
        except KeyError as e:
            logger.debug("Docker export error {0}".format(e))
        return ret

    def connect(self, version=None):
        """Connect to the Docker server."""
        # Init connection to the Docker API
        try:
            if version is None:
                ret = docker.Client(base_url='unix://var/run/docker.sock')
            else:
                ret = docker.Client(base_url='unix://var/run/docker.sock',
                                    version=version)
        except NameError:
            # docker lib not found
            return None
        try:
            ret.version()
        except requests.exceptions.ConnectionError as e:
            # Connexion error (Docker not detected)
            # Let this message in debug mode
            logger.debug("Can't connect to the Docker server (%s)" % e)
            return None
        except docker.errors.APIError as e:
            if version is None:
                # API error (Version mismatch ?)
                logger.debug("Docker API error (%s)" % e)
                # Try the connection with the server version
                version = re.search('server\:\ (.*)\)\".*\)', str(e))
                if version:
                    logger.debug("Try connection with Docker API version %s" % version.group(1))
                    ret = self.connect(version=version.group(1))
                else:
                    logger.debug("Can not retreive Docker server version")
                    ret = None
            else:
                # API error
                logger.error("Docker API error (%s)" % e)
                ret = None
        except Exception as e:
            # Others exceptions...
            # Connexion error (Docker not detected)
            logger.error("Can't connect to the Docker server (%s)" % e)
            ret = None

        # Log an info if Docker plugin is disabled
        if ret is None:
            logger.debug("Docker plugin is disable because an error has been detected")

        return ret

    def reset(self):
        """Reset/init the stats."""
        self.stats = {}

    @GlancesPlugin._log_result_decorator
    def update(self):
        """Update Docker stats using the input method."""
        # Reset stats
        self.reset()

        # Get the current Docker API client
        if not self.docker_client:
            # First time, try to connect to the server
            self.docker_client = self.connect()
            if self.docker_client is None:
                global docker_tag
                docker_tag = False

        # The Docker-py lib is mandatory
        if not docker_tag or (self.args is not None and self.args.disable_docker):
            return self.stats

        if self.input_method == 'local':
            # Update stats

            # Docker version
            # Exemple: {
            #     "KernelVersion": "3.16.4-tinycore64",
            #     "Arch": "amd64",
            #     "ApiVersion": "1.15",
            #     "Version": "1.3.0",
            #     "GitCommit": "c78088f",
            #     "Os": "linux",
            #     "GoVersion": "go1.3.3"
            # }
            try:
                self.stats['version'] = self.docker_client.version()
            except Exception as e:
                # Correct issue#649
                logger.error("{} plugin - Can not get Docker version ({})".format(self.plugin_name, e))
                return self.stats

            # Container globals information
            # Example: [{u'Status': u'Up 36 seconds',
            #            u'Created': 1420378904,
            #            u'Image': u'nginx:1',
            #            u'Ports': [{u'Type': u'tcp', u'PrivatePort': 443},
            #                       {u'IP': u'0.0.0.0', u'Type': u'tcp', u'PublicPort': 8080, u'PrivatePort': 80}],
            #            u'Command': u"nginx -g 'daemon off;'",
            #            u'Names': [u'/webstack_nginx_1'],
            #            u'Id': u'b0da859e84eb4019cf1d965b15e9323006e510352c402d2f442ea632d61faaa5'}]

            # Update current containers list
            try:
                self.stats['containers'] = self.docker_client.containers()
            except Exception as e:
                logger.error("{} plugin - Can not get containers list ({})".format(self.plugin_name, e))
                return self.stats

            # Start new thread for new container
            for container in self.stats['containers']:
                if container['Id'] not in self.thread_list:
                    # Thread did not exist in the internal dict
                    # Create it and add it to the internal dict
                    logger.debug("{} plugin - Create thread for container {}".format(self.plugin_name, container['Id'][:12]))
                    t = ThreadDockerGrabber(self.docker_client, container['Id'], self.args.time)
                    self.thread_list[container['Id']] = t
                    t.start()

            # Stop threads for non-existing containers
            nonexisting_containers = list(set(self.thread_list.keys()) - set([c['Id'] for c in self.stats['containers']]))
            for container_id in nonexisting_containers:
                # Stop the thread
                logger.debug("{} plugin - Stop thread for old container {}".format(self.plugin_name, container_id[:12]))
                self.thread_list[container_id].stop()
                # Delete the item from the dict
                del(self.thread_list[container_id])

            # Get stats for all containers
            for container in self.stats['containers']:
                # The key is the container name and not the Id
                container['key'] = self.get_key()

                # Export name (first name in the list, without the /)
                container['name'] = container['Names'][0][1:]

                container['cpu'] = self.get_docker_cpu(container['Id'], self.thread_list[container['Id']].stats)
                container['memory'] = self.get_docker_memory(container['Id'], self.thread_list[container['Id']].stats)
                container['network'] = self.get_docker_network(container['Id'], self.thread_list[container['Id']].stats)

        elif self.input_method == 'snmp':
            # Update stats using SNMP
            # Not available
            pass

        return self.stats

    def get_docker_cpu(self, container_id, all_stats):
        """Return the container CPU usage.

        Input: id is the full container id
               all_stats is the output of the stats method of the Docker API
        Output: a dict {'total': 1.49}
        """
        cpu_new = {}
        ret = {'total': 0.0}

        # Read the stats
        # For each container, you will find a pseudo-file cpuacct.stat,
        # containing the CPU usage accumulated by the processes of the container.
        # Those times are expressed in ticks of 1/USER_HZ of a second.
        # On x86 systems, USER_HZ is 100.
        try:
            cpu_new['total'] = all_stats['cpu_stats']['cpu_usage']['total_usage']
            cpu_new['system'] = all_stats['cpu_stats']['system_cpu_usage']
            cpu_new['nb_core'] = len(all_stats['cpu_stats']['cpu_usage']['percpu_usage'])
        except KeyError as e:
            # all_stats do not have CPU information
            logger.debug("Can not grab CPU usage for container {0} ({1})".format(container_id, e))
        else:
            # Previous CPU stats stored in the cpu_old variable
            if not hasattr(self, 'cpu_old'):
                # First call, we init the cpu_old variable
                self.cpu_old = {}
                try:
                    self.cpu_old[container_id] = cpu_new
                except (IOError, UnboundLocalError):
                    pass

            if container_id not in self.cpu_old:
                try:
                    self.cpu_old[container_id] = cpu_new
                except (IOError, UnboundLocalError):
                    pass
            else:
                #
                cpu_delta = float(cpu_new['total'] - self.cpu_old[container_id]['total'])
                system_delta = float(cpu_new['system'] - self.cpu_old[container_id]['system'])
                if cpu_delta > 0.0 and system_delta > 0.0:
                    ret['total'] = (cpu_delta / system_delta) * float(cpu_new['nb_core']) * 100

                # Save stats to compute next stats
                self.cpu_old[container_id] = cpu_new

        # Return the stats
        return ret

    def get_docker_memory(self, container_id, all_stats):
        """Return the container MEMORY.

        Input: id is the full container id
               all_stats is the output of the stats method of the Docker API
        Output: a dict {'rss': 1015808, 'cache': 356352,  'usage': ..., 'max_usage': ...}
        """
        ret = {}
        # Read the stats
        try:
            ret['rss'] = all_stats['memory_stats']['stats']['rss']
            ret['cache'] = all_stats['memory_stats']['stats']['cache']
            ret['usage'] = all_stats['memory_stats']['usage']
            ret['max_usage'] = all_stats['memory_stats']['max_usage']
        except KeyError as e:
            # all_stats do not have MEM information
            logger.debug("Can not grab MEM usage for container {0} ({1})".format(container_id, e))
        # Return the stats
        return ret

    def get_docker_network(self, container_id, all_stats):
        """Return the container network usage using the Docker API (v1.0 or higher).

        Input: id is the full container id
        Output: a dict {'time_since_update': 3000, 'rx': 10, 'tx': 65}.
        """
        # Init the returned dict
        network_new = {}

        # Read the rx/tx stats (in bytes)
        try:
            netiocounters = all_stats["network"]
        except KeyError as e:
            # all_stats do not have NETWORK information
            logger.debug("Can not grab NET usage for container {0} ({1})".format(container_id, e))
            # No fallback available...
            return network_new

        # Previous network interface stats are stored in the network_old variable
        if not hasattr(self, 'netiocounters_old'):
            # First call, we init the network_old var
            self.netiocounters_old = {}
            try:
                self.netiocounters_old[container_id] = netiocounters
            except (IOError, UnboundLocalError):
                pass

        if container_id not in self.netiocounters_old:
            try:
                self.netiocounters_old[container_id] = netiocounters
            except (IOError, UnboundLocalError):
                pass
        else:
            # By storing time data we enable Rx/s and Tx/s calculations in the
            # XML/RPC API, which would otherwise be overly difficult work
            # for users of the API
            network_new['time_since_update'] = getTimeSinceLastUpdate('docker_net_{0}'.format(container_id))
            network_new['rx'] = netiocounters["rx_bytes"] - self.netiocounters_old[container_id]["rx_bytes"]
            network_new['tx'] = netiocounters["tx_bytes"] - self.netiocounters_old[container_id]["tx_bytes"]
            network_new['cumulative_rx'] = netiocounters["rx_bytes"]
            network_new['cumulative_tx'] = netiocounters["tx_bytes"]

            # Save stats to compute next bitrate
            self.netiocounters_old[container_id] = netiocounters

        # Return the stats
        return network_new

    def get_user_ticks(self):
        """Return the user ticks by reading the environment variable."""
        return os.sysconf(os.sysconf_names['SC_CLK_TCK'])

    def msg_curse(self, args=None):
        """Return the dict to display in the curse interface."""
        # Init the return message
        ret = []

        # Only process if stats exist (and non null) and display plugin enable...
        if not self.stats or args.disable_docker or len(self.stats['containers']) == 0:
            return ret

        # Build the string message
        # Title
        msg = '{0}'.format('CONTAINERS')
        ret.append(self.curse_add_line(msg, "TITLE"))
        msg = ' {0}'.format(len(self.stats['containers']))
        ret.append(self.curse_add_line(msg))
        msg = ' ({0} {1})'.format('served by Docker',
                                  self.stats['version']["Version"])
        ret.append(self.curse_add_line(msg))
        ret.append(self.curse_new_line())
        # Header
        ret.append(self.curse_new_line())
        msg = '{0:>14}'.format('Id')
        ret.append(self.curse_add_line(msg))
        msg = ' {0:20}'.format('Name')
        ret.append(self.curse_add_line(msg))
        msg = '{0:>26}'.format('Status')
        ret.append(self.curse_add_line(msg))
        msg = '{0:>6}'.format('CPU%')
        ret.append(self.curse_add_line(msg))
        msg = '{0:>7}'.format('MEM')
        ret.append(self.curse_add_line(msg))
        msg = '{0:>6}'.format('Rx/s')
        ret.append(self.curse_add_line(msg))
        msg = '{0:>6}'.format('Tx/s')
        ret.append(self.curse_add_line(msg))
        msg = ' {0:8}'.format('Command')
        ret.append(self.curse_add_line(msg))
        # Data
        for container in self.stats['containers']:
            ret.append(self.curse_new_line())
            # Id
            msg = '{0:>14}'.format(container['Id'][0:12])
            ret.append(self.curse_add_line(msg))
            # Name
            name = container['Names'][0]
            if len(name) > 20:
                name = '_' + name[-19:]
            else:
                name = name[:20]
            msg = ' {0:20}'.format(name)
            ret.append(self.curse_add_line(msg))
            # Status
            status = self.container_alert(container['Status'])
            msg = container['Status'].replace("minute", "min")
            msg = '{0:>26}'.format(msg[0:25])
            ret.append(self.curse_add_line(msg, status))
            # CPU
            try:
                msg = '{0:>6.1f}'.format(container['cpu']['total'])
            except KeyError:
                msg = '{0:>6}'.format('?')
            ret.append(self.curse_add_line(msg))
            # MEM
            try:
                msg = '{0:>7}'.format(self.auto_unit(container['memory']['usage']))
            except KeyError:
                msg = '{0:>7}'.format('?')
            ret.append(self.curse_add_line(msg))
            # NET RX/TX
            for r in ['rx', 'tx']:
                try:
                    value = self.auto_unit(int(container['network'][r] // container['network']['time_since_update'] * 8)) + "b"
                    msg = '{0:>6}'.format(value)
                except KeyError:
                    msg = '{0:>6}'.format('?')
                ret.append(self.curse_add_line(msg))
            # Command
            msg = ' {0}'.format(container['Command'])
            ret.append(self.curse_add_line(msg))

        return ret

    def container_alert(self, status):
        """Analyse the container status."""
        if "Paused" in status:
            return 'CAREFUL'
        else:
            return 'OK'

class ThreadDockerGrabber(threading.Thread):
    """
    Specific thread to grab docker stats.

    stats is a dict
    """

    def __init__(self, docker_client, container_id, refresh_time=3):
        """Init the class:
        docker_client: instance of Docker-py client
        container_id: Id of the container"""
        logger.debug("docker plugin - Create thread for container {}".format(container_id[:12]))
        super(ThreadDockerGrabber, self).__init__()
        # Refresh time for sub thread
        self._refresh_time = refresh_time
        # Event needed to stop properly the thread
        self._stop = threading.Event()
        # The docker-py return stats as a stream
        self._container_id = container_id
        self._stats_stream = docker_client.stats(container_id, decode=True)
        # The class return the stats as a dict
        self._stats = {}

    def run(self):
        """Function called to grab stats.
        Infinite loop, should be stopped by calling the stop() method"""

        for i in self._stats_stream:
            self._stats = i
            # countdown = Timer(self._refresh_time)
            # while not countdown.finished() and not is_stopped:
            #     is_stopped = self.stopped()
            #     time.sleep(0.1)
            # if is_stopped:
            #     break
            time.sleep(0.1)
            if self.stopped():
                break

    @property
    def stats(self):
        return self._stats

    @stats.setter
    def stats(self, value):
        self._stats = value

    def stop(self, timeout=None):
        logger.debug("docker plugin - Close thread for container {}".format(self._container_id[:12]))
        self._stop.set()
        super(ThreadDockerGrabber, self).join(timeout)

    def stopped(self):
        return self._stop.isSet()
