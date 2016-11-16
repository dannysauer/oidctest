#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
The OP used when testing RP libraries
"""
import json
import re
import sys
import traceback
import logging
from future.backports.urllib.parse import parse_qs, urlparse
from oic.oauth2.message import Message

from oic.utils.client_management import CDB
from oic.utils.http_util import BadRequest
from oic.utils.http_util import Response
from oic.utils.http_util import NotFound
from oic.utils.http_util import ServiceError
from oic.utils.keyio import key_summary

from oidctest import UnknownTestID
from oidctest.endpoints import static
from oidctest.endpoints import display_log
from oidctest.endpoints import URLS
from oidctest.response_encoder import ResponseEncoder
from oidctest.rp import test_config
from oidctest.rp.mode import extract_mode, init_keyjar, write_jwks_uri
from oidctest.rp.mode import setup_op

from otest import Trace
from otest.conversation import Conversation
from otest.jlog import JLog

try:
    from requests.packages import urllib3
except ImportError:
    pass
else:
    urllib3.disable_warnings()

__author__ = 'rohe0002'

from mako.lookup import TemplateLookup

LOGGER = logging.getLogger("")
LOGFILE_NAME = 'oc.log'
hdlr = logging.FileHandler(LOGFILE_NAME)
base_formatter = logging.Formatter(
    "%(asctime)s %(name)s:%(levelname)s %(message)s")

CPC = ('%(asctime)s %(name)s:%(levelname)s '
       '[%(client)s,%(path)s,%(cid)s] %(message)s')
cpc_formatter = logging.Formatter(CPC)

hdlr.setFormatter(base_formatter)
LOGGER.addHandler(hdlr)
LOGGER.setLevel(logging.DEBUG)

NAME = "pyoic"

PASSWD = {
    "diana": "krall",
    "babs": "howes",
    "upper": "crust"
}

# ----------------------------------------------------------------------------

ROOT = './'

LOOKUP = TemplateLookup(directories=[ROOT + 'templates', ROOT + 'htdocs'],
                        module_directory=ROOT + 'modules',
                        input_encoding='utf-8', output_encoding='utf-8')


# ----------------------------------------------------------------------------

def get_client_address(environ):
    try:
        _addr = environ['HTTP_X_FORWARDED_FOR'].split(',')[-1].strip()
    except KeyError:
        _addr = environ['REMOTE_ADDR']
    # try:
    #     _port = environ['REMOTE_PORT']
    # except KeyError:
    #     _port = '?'
    # return "{}:{}".format(_addr, _port)
    return _addr


def rp_support_3rd_party_init_login(environ, start_response):
    resp = Response(mako_template="rp_support_3rd_party_init_login.mako",
                    template_lookup=LOOKUP,
                    headers=[])
    return resp(environ, start_response)


def rp_test_list(environ, start_response):
    resp = Response(mako_template="rp_test_list.mako",
                    template_lookup=LOOKUP,
                    headers=[])
    return resp(environ, start_response)


def registration(environ, start_response):
    resp = Response(mako_template="registration.mako",
                    template_lookup=LOOKUP,
                    headers=[])
    return resp(environ, start_response)


def generate_static_client_credentials(parameters):
    redirect_uris = parameters['redirect_uris']
    jwks_uri = str(parameters['jwks_uri'][0])
    _cdb = CDB(config.CLIENT_DB)
    static_client = _cdb.create(redirect_uris=redirect_uris,
                                # policy_uri="example.com",
                                # logo_uri="example.com",
                                jwks_uri=jwks_uri)
    return static_client['client_id'], static_client['client_secret']


def parse_path(path):
    # path should be <oper_id>/<test_id>/<endpoint> or just <endpoint>
    # if endpoint == '.well-known/webfinger'

    if path == '.well-known/webfinger':
        return {'endpoint': path}

    if path.startswith('/'):
        path = path[1:]

    p = path.split('/')
    if len(p) == 2:
        return {'oper_id': p[0], 'test_id': p[1].lower()}
    elif len(p) >= 3:
        return {'endpoint': '/'.join(p[2:]), 'oper_id': p[0],
                'test_id': p[1].lower()}
    else:
        raise ValueError('illegal path')


class Application(object):
    def __init__(self, test_conf, com_args, op_args):
        self.test_conf = test_conf
        self.op = {}
        self.com_args = com_args
        self.op_args = op_args

    def op_setup(self, environ, mode, trace, test_conf, endpoint):
        addr = get_client_address(environ)
        path = '/'.join([mode['oper_id'], mode['test_id']])

        key = "{}:{}".format(addr, path)
        #  LOGGER.debug("OP key: {}".format(key))
        try:
            _op = self.op[key]
            _op.trace = trace
            if endpoint == '.well-known/openid-configuration':
                if mode["test_id"] == 'rp-id_token-kid-absent-multiple-jwks':
                    setattr(_op, 'keys', self.op_args['marg']['keys'])
                    _op_args = {
                        'baseurl': self.op_args['baseurl'],
                        'jwks': self.op_args['marg']['jwks']
                    }
                    write_jwks_uri(_op, _op_args)
                else:
                    init_keyjar(_op, self.op_args['keyjar'], self.com_args)
                    write_jwks_uri(_op, self.op_args)
        except KeyError:
            if mode["test_id"] in ['rp-id_token-kid-absent-multiple-jwks']:
                _op_args = {}
                for param in ['baseurl', 'cookie_name', 'cookie_ttl',
                              'endpoints']:
                    _op_args[param] = self.op_args[param]
                for param in ["jwks", "keys"]:
                    _op_args[param] = self.op_args["marg"][param]
                _op = setup_op(mode, self.com_args, _op_args, trace, test_conf)
            else:
                _op = setup_op(mode, self.com_args, self.op_args, trace,
                               test_conf)
            _op.conv = Conversation(mode["test_id"], _op, None)
            _op.orig_keys = key_summary(_op.keyjar, '').split(', ')
            self.op[key] = _op

        return _op, path, key

    def application(self, environ, start_response):
        """
        :param environ: The HTTP application environment
        :param start_response: The application to run when the handling of the
            request is done
        :return: The response as a list of lines
        """

        path = environ.get('PATH_INFO', '').lstrip('/')
        response_encoder = ResponseEncoder(environ=environ,
                                           start_response=start_response)
        parameters = parse_qs(environ["QUERY_STRING"])

        session_info = {
            "addr": get_client_address(environ),
            'cookie': environ.get("HTTP_COOKIE", ''),
            'path': path,
            'parameters': parameters
        }

        jlog = JLog(LOGGER, session_info['addr'])
        jlog.info(session_info)

        if path == "robots.txt":
            return static(environ, start_response, "static/robots.txt")

        if path.startswith("static/"):
            return static(environ, start_response, path)
        elif path.startswith("log"):
            return display_log(environ, start_response, lookup=LOOKUP)
        elif path.startswith("_static/"):
            return static(environ, start_response, path)
        elif path.startswith("jwks.json"):
            try:
                mode, endpoint = extract_mode(self.op_args["baseurl"])
                trace = Trace(absolut_start=True)
                op, path, jlog.id = self.op_setup(environ, mode, trace,
                                                  self.test_conf)
                jwks = op.generate_jwks(mode)
                resp = Response(jwks,
                                headers=[('Content-Type', 'application/json')])
                return resp(environ, start_response)
            except KeyError:
                # Try to load from static file
                return static(environ, start_response, "static/jwks.json")

        trace = Trace(absolut_start=True)

        if path == "test_list":
            return rp_test_list(environ, start_response)
        elif path == "":
            return registration(environ, start_response)
        elif path == "generate_client_credentials":
            client_id, client_secret = generate_static_client_credentials(
                parameters)
            return response_encoder.return_json(
                json.dumps({"client_id": client_id,
                            "client_secret": client_secret}))
        elif path == "3rd_party_init_login":
            return rp_support_3rd_party_init_login(environ, start_response)

        # path should be <oper_id>/<test_id>/<endpoint>
        try:
            mode = parse_path(path)
        except ValueError:
            resp = BadRequest('Illegal path')
            return resp(environ, start_response)

        try:
            endpoint = mode['endpoint']
        except KeyError:
            jlog.error({'error': 'No endpoint', 'mode': mode})
            resp = BadRequest('Illegal path')
            return resp(environ, start_response)

        if endpoint == ".well-known/webfinger":
            session_info['endpoint'] = endpoint
            try:
                _p = urlparse(parameters["resource"][0])
            except KeyError:
                jlog.error({'reason': 'No resource defined'})
                resp = ServiceError("No resource defined")
                return resp(environ, start_response)

            if _p.scheme in ["http", "https"]:
                mode = parse_path(_p.path)
            elif _p.scheme == "acct":
                _l, _ = _p.path.split('@')

                _a = _l.split('.')
                if len(_a) == 2:
                    mode = {'oper_id': _a[0], "test_id": _a[1]}
                elif len(_a) > 2:
                    mode = {'oper_id': ".".join(_a[:-1]), "test_id": _a[-1]}
                else:
                    mode = {'oper_id': _a[0], "test_id": 'default'}

                trace.info(
                    'oper_id: {oper_id}, test_id: {test_id}'.format(**mode))
            else:
                _msg = "Unknown scheme: {}".format(_p.scheme)
                jlog.error({'reason': _msg})
                resp = ServiceError(_msg)
                return resp(environ, start_response)
        elif endpoint == "claim":
            authz = environ["HTTP_AUTHORIZATION"]
            try:
                assert authz.startswith("Bearer")
            except AssertionError:
                resp = BadRequest()
            else:
                tok = authz[7:]
                # mode, endpoint = extract_mode(self.op_args["baseurl"])
                _op, _, sid = self.op_setup(environ, mode, trace,
                                            self.test_conf, endpoint)
                try:
                    _claims = _op.claim_access_token[tok]
                except KeyError:
                    resp = BadRequest()
                else:
                    del _op.claim_access_token[tok]
                    _info = Message(**_claims)
                    jwt_key = _op.keyjar.get_signing_key()
                    resp = Response(_info.to_jwt(key=jwt_key,
                                                 algorithm="RS256"),
                                    content='application/jwt')
            return resp(environ, start_response)

        if mode:
            session_info.update(mode)
            jlog.id = mode['oper_id']

        try:
            _op, path, jlog.id = self.op_setup(environ, mode, trace,
                                               self.test_conf,
                                               endpoint)
        except UnknownTestID as err:
            resp = BadRequest('Unknown test ID: {}'.format(err.args[0]))
            return resp(environ, start_response)

        session_info["op"] = _op
        session_info["path"] = path
        session_info['test_conf'] = self.test_conf[session_info['test_id']]

        for regex, callback in URLS:
            match = re.search(regex, endpoint)
            if match is not None:
                trace.request("PATH: %s" % endpoint)
                trace.request("METHOD: %s" % environ["REQUEST_METHOD"])
                try:
                    trace.request(
                        "HTTP_AUTHORIZATION: %s" % environ[
                            "HTTP_AUTHORIZATION"])
                except KeyError:
                    pass

                try:
                    environ['oic.url_args'] = match.groups()[0]
                except IndexError:
                    environ['oic.url_args'] = endpoint

                jlog.info({'callback': callback.__name__})
                try:
                    return callback(environ, start_response, session_info,
                                    trace,
                                    op_arg=self.op_args, jlog=jlog)
                except Exception as err:
                    print("%s" % err)
                    message = traceback.format_exception(*sys.exc_info())
                    print(message)
                    LOGGER.exception("%s" % err)
                    resp = ServiceError("%s" % err)
                    return resp(environ, start_response)

        LOGGER.debug("unknown page: '{}'".format(endpoint))
        resp = NotFound("Couldn't find the side you asked for!")
        return resp(environ, start_response)


# ----------------------------------------------------------------------------

if __name__ == '__main__':
    import argparse

    from cherrypy import wsgiserver
    from cherrypy.wsgiserver.ssl_builtin import BuiltinSSLAdapter

    from setup import main_setup

    parser = argparse.ArgumentParser()
    parser.add_argument('-v', dest='verbose', action='store_true')
    parser.add_argument('-d', dest='debug', action='store_true')
    parser.add_argument('-p', dest='port', default=80, type=int)
    parser.add_argument('-k', dest='insecure', action='store_true')
    parser.add_argument(dest="config")
    args = parser.parse_args()

    _com_args, _op_arg, config = main_setup(args, LOOKUP)

    _app = Application(test_conf=test_config.CONF, com_args=_com_args,
                       op_args=_op_arg)
    # Setup the web server
    SRV = wsgiserver.CherryPyWSGIServer(('0.0.0.0', args.port),
                                        _app.application)

    if _op_arg["baseurl"].startswith("https"):
        SRV.ssl_adapter = BuiltinSSLAdapter(config.SERVER_CERT,
                                            config.SERVER_KEY,
                                            config.CA_BUNDLE)
        extra = " using SSL/TLS"
    else:
        extra = ""

    txt = "RP server starting listening on port:%s%s" % (args.port, extra)
    LOGGER.info(txt)
    print(txt)
    try:
        SRV.start()
    except KeyboardInterrupt:
        SRV.stop()