# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010-2011 OpenStack, LLC
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

# HTTPSClientAuthConnection code comes courtesy of ActiveState website:
# http://code.activestate.com/recipes/
#   577548-https-httplib-client-connection-with-certificate-v/

import httplib
import logging
import os
import urllib
import urlparse

from eventlet.green import socket, ssl

# See http://code.google.com/p/python-nose/issues/detail?id=373
# The code below enables glance.client standalone to work with i18n _() blocks
import __builtin__
if not hasattr(__builtin__, '_'):
    setattr(__builtin__, '_', lambda x: x)

from glance.common import auth
from glance.common import exception


class ImageBodyIterator(object):

    """
    A class that acts as an iterator over an image file's
    chunks of data.  This is returned as part of the result
    tuple from `glance.client.Client.get_image`
    """

    CHUNKSIZE = 65536

    def __init__(self, response):
        """
        Constructs the object from an HTTPResponse object
        """
        self.response = response

    def __iter__(self):
        """
        Exposes an iterator over the chunks of data in the
        image file.
        """
        while True:
            chunk = self.response.read(ImageBodyIterator.CHUNKSIZE)
            if chunk:
                yield chunk
            else:
                break


class HTTPSClientAuthConnection(httplib.HTTPSConnection):
    """
    Class to make a HTTPS connection, with support for
    full client-based SSL Authentication

    :see http://code.activestate.com/recipes/
            577548-https-httplib-client-connection-with-certificate-v/
    """

    def __init__(self, host, port, key_file, cert_file,
                 ca_file, timeout=None):
        httplib.HTTPSConnection.__init__(self, host, port, key_file=key_file,
                                         cert_file=cert_file)
        self.key_file = key_file
        self.cert_file = cert_file
        self.ca_file = ca_file
        self.timeout = timeout

    def connect(self):
        """
        Connect to a host on a given (SSL) port.
        If ca_file is pointing somewhere, use it to check Server Certificate.

        Redefined/copied and extended from httplib.py:1105 (Python 2.6.x).
        This is needed to pass cert_reqs=ssl.CERT_REQUIRED as parameter to
        ssl.wrap_socket(), which forces SSL to check server certificate against
        our client certificate.
        """
        sock = socket.create_connection((self.host, self.port), self.timeout)
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        # If there's no CA File, don't force Server Certificate Check
        if self.ca_file:
            self.sock = ssl.wrap_socket(sock, self.key_file, self.cert_file,
                                        ca_certs=self.ca_file,
                                        cert_reqs=ssl.CERT_REQUIRED)
        else:
            self.sock = ssl.wrap_socket(sock, self.key_file, self.cert_file,
                                        cert_reqs=ssl.CERT_NONE)


class BaseClient(object):

    """A base client class"""

    CHUNKSIZE = 65536
    DEFAULT_PORT = 80
    DEFAULT_DOC_ROOT = None

    def __init__(self, host, port=None, use_ssl=False, auth_tok=None,
                 creds=None, doc_root=None,
                 key_file=None, cert_file=None, ca_file=None):
        """
        Creates a new client to some service.

        :param host: The host where service resides
        :param port: The port where service resides
        :param use_ssl: Should we use HTTPS?
        :param auth_tok: The auth token to pass to the server
        :param creds: The credentials to pass to the auth plugin
        :param doc_root: Prefix for all URLs we request from host
        :param key_file: Optional PEM-formatted file that contains the private
                         key.
                         If use_ssl is True, and this param is None (the
                         default), then an environ variable
                         GLANCE_CLIENT_KEY_FILE is looked for. If no such
                         environ variable is found, ClientConnectionError
                         will be raised.
        :param cert_file: Optional PEM-formatted certificate chain file.
                          If use_ssl is True, and this param is None (the
                          default), then an environ variable
                          GLANCE_CLIENT_CERT_FILE is looked for. If no such
                          environ variable is found, ClientConnectionError
                          will be raised.
        :param ca_file: Optional CA cert file to use in SSL connections
                        If use_ssl is True, and this param is None (the
                        default), then an environ variable
                        GLANCE_CLIENT_CA_FILE is looked for.
        """
        self.host = host
        self.port = port or self.DEFAULT_PORT
        self.use_ssl = use_ssl
        self.auth_tok = auth_tok
        self.creds = creds or {}
        self.connection = None
        # doc_root can be a nullstring, which is valid, and why we
        # cannot simply do doc_root or self.DEFAULT_DOC_ROOT below.
        self.doc_root = (doc_root if doc_root is not None
                         else self.DEFAULT_DOC_ROOT)
        self.auth_plugin = self.make_auth_plugin(self.creds)
        self.connect_kwargs = {}

        if use_ssl:
            if not key_file:
                if not os.environ.get('GLANCE_CLIENT_KEY_FILE'):
                    msg = _("You have selected to use SSL in connecting, "
                            "however you have failed to supply either a "
                            "key_file parameter or set the "
                            "GLANCE_CLIENT_KEY_FILE environ variable")
                    raise exception.ClientConnectionError(msg)
                key_file = os.environ.get('GLANCE_CLIENT_KEY_FILE')

            if not os.path.exists(key_file):
                msg = _("The key file you specified %s does not "
                        "exist") % key_file
                raise exception.ClientConnectionError(msg)
            self.connect_kwargs['key_file'] = key_file

            if not cert_file:
                if not os.environ.get('GLANCE_CLIENT_CERT_FILE'):
                    msg = _("You have selected to use SSL in connecting, "
                            "however you have failed to supply either a "
                            "cert_file parameter or set the "
                            "GLANCE_CLIENT_CERT_FILE environ variable")
                    raise exception.ClientConnectionError(msg)
                cert_file = os.environ.get('GLANCE_CLIENT_CERT_FILE')

            if not os.path.exists(cert_file):
                msg = _("The key file you specified %s does not "
                        "exist") % cert_file
                raise exception.ClientConnectionError(msg)
            self.connect_kwargs['cert_file'] = cert_file

            if not ca_file:
                ca_file = os.environ.get('GLANCE_CLIENT_CA_FILE')

            self.connect_kwargs['ca_file'] = ca_file

    def set_auth_token(self, auth_tok):
        """
        Updates the authentication token for this client connection.
        """
        # FIXME(sirp): Nova image/glance.py currently calls this. Since this
        # method isn't really doing anything useful[1], we should go ahead and
        # rip it out, first in Nova, then here. Steps:
        #
        #       1. Change auth_tok in Glance to auth_token
        #       2. Change image/glance.py in Nova to use client.auth_token
        #       3. Remove this method
        #
        # [1] http://mail.python.org/pipermail/tutor/2003-October/025932.html
        self.auth_tok = auth_tok

    def configure_from_url(self, url):
        """
        Setups the connection based on the given url.

        The form is:

            <http|https>://<host>:port/doc_root
        """
        parsed = urlparse.urlparse(url)
        self.use_ssl = parsed.scheme == 'https'
        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.doc_root = parsed.path

    def make_auth_plugin(self, creds):
        strategy = creds.get('strategy', 'noauth')
        plugin_class = auth.get_plugin_from_strategy(strategy)
        plugin = plugin_class(creds)
        return plugin

    def get_connection_type(self):
        """
        Returns the proper connection type
        """
        if self.use_ssl:
            return HTTPSClientAuthConnection
        else:
            return httplib.HTTPConnection

    def _authenticate(self, force_reauth=False):
        auth_plugin = self.auth_plugin

        if not auth_plugin.is_authenticated or force_reauth:
            auth_plugin.authenticate()

        self.auth_tok = auth_plugin.auth_token

        management_url = auth_plugin.management_url
        if management_url:
            self.configure_from_url(management_url)

    def do_request(self, method, action, body=None, headers=None,
                   params=None):
        headers = headers or {}

        if not self.auth_tok:
            self._authenticate()

        try:
            return self._do_request(
                method, action, body=body, headers=headers, params=params)
        except exception.NotAuthorized:
            self._authenticate(force_reauth=True)
            try:
                return self._do_request(
                    method, action, body=body, headers=headers, params=params)
            except exception.NotAuthorized:
                raise

    def _do_request(self, method, action, body=None, headers=None,
                   params=None):
        """
        Connects to the server and issues a request.  Handles converting
        any returned HTTP error status codes to OpenStack/Glance exceptions
        and closing the server connection. Returns the result data, or
        raises an appropriate exception.

        :param method: HTTP method ("GET", "POST", "PUT", etc...)
        :param action: part of URL after root netloc
        :param body: string of data to send, or None (default)
        :param headers: mapping of key/value pairs to add as headers
        :param params: dictionary of key/value pairs to add to append
                             to action

        :note

        If the body param has a read attribute, and method is either
        POST or PUT, this method will automatically conduct a chunked-transfer
        encoding and use the body as a file object, transferring chunks
        of data using the connection's send() method. This allows large
        objects to be transferred efficiently without buffering the entire
        body in memory.
        """
        if type(params) is dict:

            # remove any params that are None
            for (key, value) in params.items():
                if value is None:
                    del params[key]

            action += '?' + urllib.urlencode(params)

        try:
            connection_type = self.get_connection_type()
            headers = headers or {}

            if 'x-auth-token' not in headers and self.auth_tok:
                headers['x-auth-token'] = self.auth_tok

            c = connection_type(self.host, self.port, **self.connect_kwargs)

            if self.doc_root:
                action = '/'.join([self.doc_root, action.lstrip('/')])

            # Do a simple request or a chunked request, depending
            # on whether the body param is a file-like object and
            # the method is PUT or POST
            if hasattr(body, 'read') and method.lower() in ('post', 'put'):
                # Chunk it, baby...
                c.putrequest(method, action)

                for header, value in headers.items():
                    c.putheader(header, value)
                c.putheader('Transfer-Encoding', 'chunked')
                c.endheaders()

                chunk = body.read(self.CHUNKSIZE)
                while chunk:
                    c.send('%x\r\n%s\r\n' % (len(chunk), chunk))
                    chunk = body.read(self.CHUNKSIZE)
                c.send('0\r\n\r\n')
            else:
                # Simple request...
                c.request(method, action, body, headers)
            res = c.getresponse()
            status_code = self.get_status_code(res)
            if status_code in (httplib.OK,
                               httplib.CREATED,
                               httplib.ACCEPTED,
                               httplib.NO_CONTENT):
                return res
            elif status_code == httplib.UNAUTHORIZED:
                raise exception.NotAuthorized(res.read())
            elif status_code == httplib.FORBIDDEN:
                raise exception.NotAuthorized(res.read())
            elif status_code == httplib.NOT_FOUND:
                raise exception.NotFound(res.read())
            elif status_code == httplib.CONFLICT:
                raise exception.Duplicate(res.read())
            elif status_code == httplib.BAD_REQUEST:
                raise exception.Invalid(res.read())
            elif status_code == httplib.MULTIPLE_CHOICES:
                raise exception.MultipleChoices(body=res.read())
            elif status_code == httplib.INTERNAL_SERVER_ERROR:
                raise Exception("Internal Server error: %s" % res.read())
            else:
                raise Exception("Unknown error occurred! %s" % res.read())

        except (socket.error, IOError), e:
            raise exception.ClientConnectionError(e)

    def get_status_code(self, response):
        """
        Returns the integer status code from the response, which
        can be either a Webob.Response (used in testing) or httplib.Response
        """
        if hasattr(response, 'status_int'):
            return response.status_int
        else:
            return response.status

    def _extract_params(self, actual_params, allowed_params):
        """
        Extract a subset of keys from a dictionary. The filters key
        will also be extracted, and each of its values will be returned
        as an individual param.

        :param actual_params: dict of keys to filter
        :param allowed_params: list of keys that 'actual_params' will be
                               reduced to
        :retval subset of 'params' dict
        """
        try:
            # expect 'filters' param to be a dict here
            result = dict(actual_params.get('filters'))
        except TypeError:
            result = {}

        for allowed_param in allowed_params:
            if allowed_param in actual_params:
                result[allowed_param] = actual_params[allowed_param]

        return result
