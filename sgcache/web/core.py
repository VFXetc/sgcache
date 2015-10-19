from types import GeneratorType
import copy
import json
import logging
import os
import time

from flask import Flask, request, Response, stream_with_context, g, redirect
from werkzeug.http import remove_hop_by_hop_headers
import requests
import sqlalchemy as sa
import yaml

from .. import config
from ..cache import Cache
from ..exceptions import Passthrough, Fault
from ..logs import setup_logs, log_globals
from ..schema import Schema
from ..utils import get_shotgun_kwargs

log = logging.getLogger(__name__)


app = Flask(__name__)
app.config.from_object(config)

db = sa.create_engine(app.config['SQLA_URL'], echo=bool(app.config['SQLA_ECHO']))

# Setup logging *after* SQLA so that it can deal with its handlers.
setup_logs(app)

schema = Schema.from_yaml(app.config['SCHEMA'])
cache = Cache(db, schema) # SQL DDL is executed here; watch out!

# Get the fallback server from shotgun_api3_registry.
FALLBACK_SERVER = get_shotgun_kwargs()['base_url'].strip('/')
FALLBACK_URL = FALLBACK_SERVER + '/api3/json'

# We use one HTTP session for everything.
http_session = requests.Session()


class ReturnResponse(ValueError):
    pass

class ReturnPassthroughError(ReturnResponse):
    pass


def passthrough(payload=None, params=None, raise_exceptions=True, stream=False, final=False):

    # Remove headers which we should not pass on.
    headers = [(k, v) for k, v in request.headers.iteritems() if k.lower() != 'host']
    remove_hop_by_hop_headers(headers)
    headers = dict(headers)

    if payload is None:
        payload = copy.deepcopy(g.api3_payload)
    if params is not None:
        payload['params'][-1] = params

    if not isinstance(payload, basestring):
        payload = json.dumps(payload)

    http_response = http_session.post(FALLBACK_URL, data=payload, headers=headers, stream=stream)

    if http_response.status_code != 200:
        raise ReturnPassthroughError(Response(
            http_response.iter_content(8192) if stream else http_response.text,
            http_response.status_code,
            mimetype='application/json',
        ))

    if stream or final:
        return Response(
            http_response.iter_content(8192) if stream else http_response.text,
            200,
            mimetype='application/json'
        )

    else:
        response_data = json.loads(http_response.text)
        if raise_exceptions and 'exception' in response_data:
            raise ReturnResponse(http_response.text, 200, [('Content-Type', 'application/json')])
        else:
            return response_data


# This is used by our shotgun_api3_registry to assert that the cache is up.
# In the future we may have something with a bit more information, or have the
# "info" method return a bit more.
@app.route('/ping')
def on_ping():
    return 'pong', 200, [('Content-Type', 'text/plain')]


# Forward detail requests through to the real thing.
@app.route('/')
@app.route('/detail/<path:path>')
@app.route('/page/<path:path>')
def forward_details(path=''):
    url = FALLBACK_SERVER + request.path
    return redirect(url)



@app.route('/api3/json', methods=['POST'])
@app.route('/<path:params>/api3/json', methods=['POST'])
def json_api(params=None):

    payload = g.api3_payload = json.loads(request.data)

    if not isinstance(payload, dict):
        return '', 400, []

    try:
        method_name = payload['method_name']
        params = payload['params']
        auth_params = params[0] if params else {}
        method_params = params[1] if len(params) > 1 else {}
    except KeyError:
        return '', 400, []

    # Log the base of the request.
    headline_chunks = ['Starting %s' % method_name]
    if isinstance(method_params, dict):
        entity_type = method_params.get('type')
        if entity_type:
            headline_chunks.append('on %s' % entity_type)
    elif method_name == 'batch' and isinstance(method_params, list):
        batch_chunks = []
        batch_counts = {}
        for x in method_params:
            batch_counts[x['request_type']] = batch_counts.get(x['request_type'], 0) + 1
        for name, count in sorted(batch_counts.iteritems(), key=lambda x: x[1]):
            batch_chunks.append('%d %s%s' % (count, name, 's' if count != 1 else ''))
        headline_chunks.append('(%s)' % ', '.join(batch_chunks))
    script_name = auth_params.get('script_name')
    if script_name:
        headline_chunks.append('by script "%s"' % script_name)
        sudo_as_login = auth_params.get('sudo_as_login')
        if sudo_as_login:
            headline_chunks.append('as user "%s"' % sudo_as_login)
    else:
        user_login = auth_params.get('user_login')
        if user_login:
            headline_chunks.append('by user "%s"' % user_login)
    log.info(' '.join(headline_chunks))

    try:
        method = _api3_methods[method_name]
    except KeyError as e:
        if app.debug and method_params:
            detail = ':\n' + json.dumps(method_params, sort_keys=True, indent=4)
        else:
            detail = ''
        log.info('Passing through "%s" due to unknown API method%s' % (method_name, detail))
        return passthrough(stream=True)

    start_time = time.time()

    try:

        result = method(method_params)

        # If the method is a generator, then it will yield the params to use
        # for passthrough requests, and we send it back the results. We do
        # this to generalize our handling of requests so that the same code
        # will work in batch mode.
        if isinstance(result, GeneratorType):
            coroutine = result
            passthrough_params = next(coroutine)
            try:
                passthrough_results = passthrough(params=passthrough_params)
            except:
                coroutine.throw(*sys.exc_info())
            else:
                result = coroutine.send(passthrough_results)
                for x in coroutine:
                    log.warning('Extra yielded from generator method: %r' % x)


    # Exceptions as control flow.
    except ReturnResponse as e:
        result = e.args

    # An (emulated) Shotgun fault has occoured.
    except Fault as e:
        log.warning('%s (%s): %s' % (e.__class__.__name__, e.code, e.args[0]))
        result_data = {
            'exception': True,
            'error_code': e.code,
            'message': e.args[0],
        }
        # Shotgun does still return a 200 here.
        result = json.dumps(res_data), 200, [('Content-Type', 'application/json')]

    # Some operation has resulted in an abortive request to pass through the request.
    except Passthrough as e:
        if app.debug and method_params:
            detail = ':\n' + json.dumps(method_params, sort_keys=True, indent=4)
        else:
            detail = ''
        log.info('Passing through %s due to %s("%s")%s' % (
            method_name,
            e.__class__.__name__,
            e,
            detail,
        ))
        result = passthrough(stream=True)

    if isinstance(result, dict):
        num_entities = len(result['entities']) if 'entities' in result else None
    else:
        num_entities = None

    # If the results are not a Result (or tuple/list with one), then treat
    # it as raw data to serialize.
    if not (isinstance(result, Response) or (isinstance(result, (list, tuple)) and isinstance(result[0], Response))):
        result = json.dumps(result), 200, [('Content-Type', 'application/json')]

    elapsed_ms = 1000 * (time.time() - start_time)
    log.info('Returned %sin %.1fms' % (
        '%s %ss ' % (num_entities, entity_type) if num_entities else '',
        elapsed_ms
    ))
    log_globals.skip_http_log = True

    return result



# For handing a Flask stream to Requests; the iter API is likely
# throwing Requests off.
class _StreamReadWrapper(object):
    def __init__(self, fh):
        self.read = fh.read

@app.route('/file_serve/<path:path>', methods=['GET', 'POST'])
@app.route('/thumbnail/<path:path>', methods=['GET', 'POST'])
@app.route('/upload/<path:path>', methods=['GET', 'POST'])
def proxy(path):

    url = FALLBACK_SERVER + request.path

    # Strip out the hop-by-hop headers, AND the host (since that likely points
    # to the cache and not Shotgun).
    headers = [(k, v) for k, v in request.headers.items() if k.lower() != 'host']
    remove_hop_by_hop_headers(headers)

    remote_response = http_session.request(request.method, url,
        data=_StreamReadWrapper(request.stream),
        params=request.args,
        headers=dict(headers),
        stream=True,
    )

    headers = remote_response.headers.items()
    remove_hop_by_hop_headers(headers)

    return Response(
        remote_response.iter_content(8192),
        status=remote_response.status_code,
        headers=headers,
        direct_passthrough=True, # Don't encode it.
    )



# Register api methods
_api3_methods = {}
def api3_method(func):
    _api3_methods[func.__name__] = func
    return func

from . import api3
