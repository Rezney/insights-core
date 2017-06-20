import optparse
import urllib2
from . import client
from .constants import InsightsConstants as constants
from .auto_config import try_auto_configuration
from .client_config import parse_config_file, InsightsClient, set_up_options


def get_version():
    """
        returns (str): version of the client
    """
    return constants.version


def run(egg_url=constants.egg_path,
        gpg_key=constants.default_egg_gpg_key,
        collection_format='json',
        options=None,
        config=None,
        skip_update=False,
        skip_verify=False,
        skip_upload=False):
    '''
        do everything
    '''
    new_egg = None
    verification = True
    results = None
    if not skip_update:
        new_egg = fetch(egg_url)
    if new_egg and not skip_verify:
        verification = verify(new_egg, gpg_key)
    if verification:
        results = collect(collection_format, options, config)
    else:
        results = False
    if not skip_upload:
        return upload(results)
    else:
        return results


def parse_options():
    '''
        returns (tuple): returns a tuple with configparser and argparser options
    '''
    parser = optparse.OptionParser()
    set_up_options(parser)
    options, args = parser.parse_args()
    if len(args) > 0:
        parser.error("Unknown arguments: %s" % args)
    return parse_config_file(options.conf), options


def fetch(egg_url=constants.egg_path):
    """
        returns (str): path to new egg.  None if no update.
    """
    current_etag = None
    import os
    if os.path.isfile(constants.core_etag_file):
        f = open(constants.core_etag_file, 'r')
        current_etag = f.read().strip()
        f.close()

    request = urllib2.Request(egg_url)
    if current_etag:
        request.add_header('If-None-Match', current_etag)
    opener = urllib2.build_opener(DefaultErrorHandler())
    datastream = opener.open(request)
    data = datastream.read()

    f = open(constants.core_etag_file, 'w')
    f.write(datastream.headers.dict['etag'])
    f.close()

    if data:
        import tempfile
        tmpdir = tempfile.mkdtemp()
        tmp_egg_path = os.path.join(tmpdir, 'insights-core.egg')

        with open(tmp_egg_path, 'wb') as handle:
            handle.write(data)

        return tmp_egg_path

    else:
        return None


class DefaultErrorHandler(urllib2.HTTPDefaultErrorHandler):
        def http_error_default(self, req, fp, code, msg, headers):
            result = urllib2.HTTPError(req.get_full_url(), code, msg, headers, fp)
            result.status = code
            return result


def verify(egg_path, gpg_key=constants.default_egg_gpg_key):
    """
        returns (dict): {'gpg': if the egg checks out,
                         'stderr': error message if present,
                         'stdout': stdout,
                         'rc': return code}
    """
    from subprocess import Popen, PIPE
    process = Popen(['gpg', '--verify', gpg_key, egg_path], stdout=PIPE, stderr=PIPE)
    stdout, stderr = process.communicate()
    rc = process.returncode
    success = True if rc == 0 else False
    return {'gpg': success, 'stderr': stderr, 'stdout': stdout, 'rc': rc}


def fetch_rules(options=None, config=None):
    '''
        returns (dict): new client rules
    '''
    InsightsClient.config, InsightsClient.options = parse_options()
    try_auto_configuration()
    return client.fetch_rules()


def collect(format="json", options=None, config=None):
    '''
        returns (str, json): will return a string path to archive, or json facts
    '''
    InsightsClient.config, InsightsClient.options = parse_options()
    try_auto_configuration()
    if options:
        for key in options:
            setattr(InsightsClient.options, key, options[key])
    if config:
        for new_config_var in config:
            InsightsClient.config.set(constants.app_name, new_config_var, config[new_config_var])
    return client.collect()


def upload(path):
    return client.upload(path)