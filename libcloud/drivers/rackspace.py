# Licensed to libcloud.org under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# libcloud.org licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from libcloud.types import NodeState, InvalidCredsException, Provider
from libcloud.base import ConnectionUserAndKey, Response, NodeDriver, Node, NodeSize, NodeImage
from libcloud.interface import INodeDriver

from zope.interface import implements

import urlparse

from xml.etree import ElementTree as ET
from xml.parsers.expat import ExpatError

NAMESPACE = 'http://docs.rackspacecloud.com/servers/api/v1.0'

class RackspaceResponse(Response):

    def parse_body(self):
        if not self.body:
            return None
        return ET.XML(self.body)

    def parse_error(self):
        # TODO: fixup, Rackspace only uses response codes really!
        try:
            object = ET.XML(self.body)
            return "; ".join([ err.text
                               for err in
                               object.findall('error') ])
        except ExpatError:
            return self.body


class RackspaceConnection(ConnectionUserAndKey):
    api_version = 'v1.0'
    host = 'auth.api.rackspacecloud.com'
    auth_host = None
    endpoint = None
    path = None
    token = None

    responseCls = RackspaceResponse

    def default_headers(self):
        return {'X-Auth-Token': self.token,
                 'Accept': 'application/xml' }

    def _authenticate(self):
        # TODO: Fixup for when our token expires (!!!)
        self.auth_host = self.host

        self.connection.request(method='GET', url='/%s' % self.api_version,
                                       headers={'X-Auth-User': self.user_id,
                                                'X-Auth-Key': self.key})
        resp = self.connection.getresponse()
        headers = dict(resp.getheaders())
        self.token = headers.get('x-auth-token')
        self.endpoint = headers.get('x-server-management-url')
        if not self.token or not self.endpoint:
            raise InvalidCredsException()

        scheme, server, self.path, param, query, fragment = (
            urlparse.urlparse(self.endpoint)
        )

        # Okay, this is evil.  We replace host here, and then re-run 
        # super connect() to re-setup the connection classes with the 'correct'
        # host.
        self.host = server

        if scheme is "https" and self.secure is not 1:
            # TODO: Custom exception (?)
            raise InvalidCredsException()
            
        super(RackspaceConnection, self).connect()

    def connect(self, host=None, port=None):
        super(RackspaceConnection, self).connect()
        self._authenticate()

    def request(self, action, params={}, data='', method='GET'):
        action = self.path + action
        return super(RackspaceConnection, self).request(action=action, params=params, data=data, method=method)
        
class RackspaceNodeDriver(NodeDriver):

    connectionCls = RackspaceConnection
    type = Provider.RACKSPACE
    name = 'Rackspace'

    NODE_STATE_MAP = {  'BUILD': NodeState.PENDING,
                        'ACTIVE': NodeState.RUNNING,
                        'SUSPENDED': NodeState.TERMINATED,
                        'QUEUE_RESIZE': NodeState.PENDING,
                        'PREP_RESIZE': NodeState.PENDING,
                        'RESCUE': NodeState.PENDING,
                        'REBUILD': NodeState.PENDING,
                        'REBOOT': NodeState.REBOOTING,
                        'HARD_REBOOT': NodeState.REBOOTING}

    def list_nodes(self):
        #print self.connection.request('/servers/detail').body
        return self.to_nodes(self.connection.request('/servers/detail').object)

    def list_sizes(self):
        return self.to_sizes(self.connection.request('/flavors/detail').object)

    def list_images(self):
        return self.to_images(self.connection.request('/images/detail').object)

    def create_node(self, name, image, size):
        raise NotImplemented

    def reboot_node(self, node):
        # TODO: Hard Reboots should be supported too!
        resp = self._node_action(node, ['reboot', ('type', 'SOFT')])
        return resp.status == 202

    def _node_action(self, node, body):
        ### consider this from old code:
        #         data = ('<%s xmlns="%s" %s/>'
        #        % (verb, NAMESPACE,
        #           ' '.join(['%s="%s"' % item for item in params.items()])))
        if isinstance(body, list):
            attr = ' '.join(['%s="%s"' % (item[0], item[1]) for item in body[1:]])
            body = '<%s xmlns="%s" %s/>' % (body[0], NAMESPACE, attr)
        uri = '/servers/%s/action' % (node.id)
        resp = self.connection.request(uri, method='POST', body=body)
        return resp

    def to_nodes(self, object):
        node_elements = self._findall(object, 'server')
        return [ self._to_node(el) for el in node_elements ]

    def _fixxpath(self, xpath):
        # ElementTree wants namespaces in its xpaths, so here we add them.
        return "/".join(["{%s}%s" % (NAMESPACE, e) for e in xpath.split("/")])

    def _findall(self, element, xpath):
        return element.findall(self._fixxpath(xpath))

    def _to_node(self, el):
        def get_ips(el):
            return [ip.get('addr') for ip in el.children()]
        
        public_ip = get_ips(self._findall(el, 
                                          'addresses/public'))
        private_ip = get_ips(self._findall(el, 
                                          'addresses/private'))
        n = Node(id=el.get('id'),
                 name=el.get('name'),
                 state=el.get('status'),
                 public_ip=public_ip,
                 private_ip=private_ip,
                 driver=self.connection.driver)
        return n

    def to_sizes(self, object):
        elements = self._findall(object, 'flavor')
        return [ self._to_size(el) for el in elements ]

    def _to_size(self, el):
        s = NodeSize(id=el.get('id'),
                     name=el.get('name'),
                     ram=int(el.get('ram')),
                     disk=int(el.get('disk')),
                     bandwidth=None, # XXX: needs hardcode
                     price=None, # XXX: needs hardcode,
                     driver=self.connection.driver)
        return s

    def to_images(self, object):
        elements = self._findall(object, "image")
        return [ self._to_image(el) for el in elements if el.get('status') == 'ACTIVE']

    def _to_image(self, el):
        i = NodeImage(id=el.get('id'),
                     name=el.get('name'),
                     driver=self.connection.driver)
        return i;
