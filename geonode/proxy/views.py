#########################################################################
#
# Copyright (C) 2016 OSGeo
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################
import io
import os
import re
import gzip
import shutil
import logging
import tempfile
import traceback

from hyperlink import URL
from urllib.parse import urlparse, urlsplit, urljoin

from django.conf import settings
from django.template import loader
from django.http import HttpResponse
from django.views.generic import View
from distutils.version import StrictVersion
from django.http.request import validate_host
from django.utils.translation import ugettext as _
from django.views.decorators.csrf import requires_csrf_token

from geonode.layers.models import Dataset
from geonode.upload.models import Upload
from geonode.base.models import ResourceBase
from geonode.storage.manager import storage_manager
from geonode.utils import (
    resolve_object,
    check_ogc_backend,
    get_dir_time_suffix,
    zip_dir,
    get_headers,
    http_client,
    json_response)
from geonode.base.enumerations import LINK_TYPES as _LT

from geonode import geoserver  # noqa
from geonode.base import register_event

TIMEOUT = 30

LINK_TYPES = [L for L in _LT if L.startswith("OGC:")]

logger = logging.getLogger(__name__)


ows_regexp = re.compile(
    r"^(?i)(version)=(\d\.\d\.\d)(?i)&(?i)request=(?i)(GetCapabilities)&(?i)service=(?i)(\w\w\w)$")


@requires_csrf_token
def proxy(request, url=None, response_callback=None,
          sec_chk_hosts=True, sec_chk_rules=True, timeout=None,
          allowed_hosts=[], **kwargs):
    # Request default timeout
    if not timeout:
        timeout = TIMEOUT

    # Security rules and settings
    PROXY_ALLOWED_HOSTS = getattr(settings, 'PROXY_ALLOWED_HOSTS', ())

    # Sanity url checks
    if 'url' not in request.GET and not url:
        return HttpResponse("The proxy service requires a URL-encoded URL as a parameter.",
                            status=400,
                            content_type="text/plain"
                            )

    raw_url = url or request.GET['url']
    raw_url = urljoin(
        settings.SITEURL,
        raw_url) if raw_url.startswith("/") else raw_url
    url = urlsplit(raw_url)
    scheme = str(url.scheme)
    locator = str(url.path)
    if url.query != "":
        locator += f"?{url.query}"
    if url.fragment != "":
        locator += f"#{url.fragment}"

    # White-Black Listing Hosts
    site_url = urlsplit(settings.SITEURL)
    if sec_chk_hosts and not settings.DEBUG:

        # Attach current SITEURL
        if site_url.hostname not in PROXY_ALLOWED_HOSTS:
            PROXY_ALLOWED_HOSTS += (site_url.hostname, )

        # Attach current hostname
        if check_ogc_backend(geoserver.BACKEND_PACKAGE):
            from geonode.geoserver.helpers import ogc_server_settings
            hostname = (
                ogc_server_settings.hostname,
            ) if ogc_server_settings else ()
            if hostname not in PROXY_ALLOWED_HOSTS:
                PROXY_ALLOWED_HOSTS += hostname

        # Check OWS regexp
        if url.query and ows_regexp.match(url.query):
            ows_tokens = ows_regexp.match(url.query).groups()
            if len(ows_tokens) == 4 and 'version' == ows_tokens[0] and StrictVersion(
                    ows_tokens[1]) >= StrictVersion("1.0.0") and StrictVersion(
                        ows_tokens[1]) <= StrictVersion("3.0.0") and ows_tokens[2].lower() in (
                            'getcapabilities') and ows_tokens[3].upper() in ('OWS', 'WCS', 'WFS', 'WMS', 'WPS', 'CSW'):
                if url.hostname not in PROXY_ALLOWED_HOSTS:
                    PROXY_ALLOWED_HOSTS += (url.hostname, )

        # Check Remote Services base_urls
        from geonode.services.models import Service
        for _s in Service.objects.all():
            _remote_host = urlsplit(_s.base_url).hostname
            PROXY_ALLOWED_HOSTS += (_remote_host, )

        if not validate_host(
                url.hostname, PROXY_ALLOWED_HOSTS):
            return HttpResponse("DEBUG is set to False but the host of the path provided to the proxy service"
                                " is not in the PROXY_ALLOWED_HOSTS setting.",
                                status=403,
                                content_type="text/plain"
                                )

    # Security checks based on rules; allow only specific requests
    if sec_chk_rules:
        # TODO: Not yet implemented
        pass

    # Collecting headers and cookies
    headers, access_token = get_headers(request, url, raw_url, allowed_hosts=allowed_hosts)

    # Inject access_token if necessary
    parsed = urlparse(raw_url)
    parsed._replace(path=locator.encode('utf8'))
    if parsed.netloc == site_url.netloc and scheme != site_url.scheme:
        parsed = parsed._replace(scheme=site_url.scheme)

    _url = parsed.geturl()

    # Some clients / JS libraries generate URLs with relative URL paths, e.g.
    # "http://host/path/path/../file.css", which the requests library cannot
    # currently handle (https://github.com/kennethreitz/requests/issues/2982).
    # We parse and normalise such URLs into absolute paths before attempting
    # to proxy the request.
    _url = URL.from_text(_url).normalize().to_text()

    if request.method == "GET" and access_token and 'access_token' not in _url:
        query_separator = '&' if '?' in _url else '?'
        _url = f'{_url}{query_separator}access_token={access_token}'

    _data = request.body.decode('utf-8')

    # Avoid translating local geoserver calls into external ones
    if check_ogc_backend(geoserver.BACKEND_PACKAGE):
        from geonode.geoserver.helpers import ogc_server_settings
        _url = _url.replace(
            f'{settings.SITEURL}geoserver',
            ogc_server_settings.LOCATION.rstrip('/'))
        _data = _data.replace(
            f'{settings.SITEURL}geoserver',
            ogc_server_settings.LOCATION.rstrip('/'))

    response, content = http_client.request(
        _url,
        method=request.method,
        data=_data.encode('utf-8'),
        headers=headers,
        timeout=timeout,
        user=request.user)
    if response is None:
        return HttpResponse(
            content=content,
            reason=content,
            status=500)
    content = response.content or response.reason
    status = response.status_code
    response_headers = response.headers
    content_type = response.headers.get('Content-Type')

    if status >= 400:
        _response = HttpResponse(
            content=content,
            reason=content,
            status=status,
            content_type=content_type)
        return fetch_response_headers(_response, response_headers)

    # decompress GZipped responses if not enabled
    # if content and response and response.getheader('Content-Encoding') == 'gzip':
    if content and content_type and content_type == 'gzip':
        buf = io.BytesIO(content)
        with gzip.GzipFile(fileobj=buf) as f:
            content = f.read()
        buf.close()

    PLAIN_CONTENT_TYPES = [
        'text',
        'plain',
        'html',
        'json',
        'xml',
        'gml'
    ]
    for _ct in PLAIN_CONTENT_TYPES:
        if content_type and _ct in content_type and not isinstance(content, str):
            try:
                content = content.decode()
                break
            except Exception:
                pass

    if response and response_callback:
        kwargs = {} if not kwargs else kwargs
        kwargs.update({
            'response': response,
            'content': content,
            'status': status,
            'response_headers': response_headers,
            'content_type': content_type
        })
        return response_callback(**kwargs)
    else:
        # If we get a redirect, let's add a useful message.
        if status and status in (301, 302, 303, 307):
            _response = HttpResponse((f"This proxy does not support redirects. The server in '{url}' "
                                      f"asked for a redirect to '{response.getheader('Location')}'"),
                                     status=status,
                                     content_type=content_type
                                     )
            _response['Location'] = response.getheader('Location')
            return fetch_response_headers(_response, response_headers)
        else:
            def _get_message(text):
                _s = text
                if isinstance(text, bytes):
                    _s = text.decode("utf-8", "replace")
                try:
                    found = re.search('<b>Message</b>(.+?)</p>', _s).group(1).strip()
                except Exception:
                    found = _s
                return found

            _response = HttpResponse(
                content=content,
                reason=_get_message(content) if status not in (200, 201) else None,
                status=status,
                content_type=content_type)
            return fetch_response_headers(_response, response_headers)


def download(request, resourceid, sender=Dataset):

    _not_authorized = _("You are not authorized to download this resource.")
    _not_permitted = _("You are not permitted to save or edit this resource.")
    _no_files_found = _("No files have been found for this resource. Please, contact a system administrator.")

    instance = resolve_object(request,
                              sender,
                              {'pk': resourceid},
                              permission='base.download_resourcebase',
                              permission_msg=_not_permitted)

    if isinstance(instance, ResourceBase):
        # Create Target Folder
        dirpath = tempfile.mkdtemp(dir=settings.STATIC_ROOT)
        dir_time_suffix = get_dir_time_suffix()
        target_folder = os.path.join(dirpath, dir_time_suffix)
        if not os.path.exists(target_folder):
            os.makedirs(target_folder)

        dataset_files = []
        try:
            files = instance.resourcebase_ptr.files
            # Copy all Dataset related files into a temporary folder
            for file_path in files:
                if storage_manager.exists(file_path):
                    dataset_files.append(file_path)
                    filename = os.path.basename(file_path)
                    with open(f"{target_folder}/{filename}", 'wb+') as f:
                        f.write(storage_manager.open(file_path).read())
                else:
                    return HttpResponse(
                        loader.render_to_string(
                            '401.html',
                            context={
                                'error_title': _("No files found."),
                                'error_message': _no_files_found
                            },
                            request=request), status=404)

            # Check we can access the original files
            if not dataset_files:
                return HttpResponse(
                    loader.render_to_string(
                        '401.html',
                        context={
                            'error_title': _("No files found."),
                            'error_message': _no_files_found
                        },
                        request=request), status=404)

            # ZIP everything and return
            target_file_name = "".join([instance.name, ".zip"])
            target_file = os.path.join(dirpath, target_file_name)
            zip_dir(target_folder, target_file)
            register_event(request, 'download', instance)
            response = HttpResponse(
                content=open(target_file, mode='rb'),
                status=200,
                content_type="application/zip")
            response['Content-Disposition'] = f'attachment; filename="{target_file_name}"'
            return response
        except (NotImplementedError, Upload.DoesNotExist):
            traceback.print_exc()
            tb = traceback.format_exc()
            logger.debug(tb)
            return HttpResponse(
                loader.render_to_string(
                    '401.html',
                    context={
                        'error_title': _("No files found."),
                        'error_message': _no_files_found
                    },
                    request=request), status=404)
        finally:
            if target_folder is not None:
                shutil.rmtree(target_folder, ignore_errors=True)
    return HttpResponse(
        loader.render_to_string(
            '401.html',
            context={
                'error_title': _("Not Authorized"),
                'error_message': _not_authorized
            },
            request=request), status=403)


class OWSListView(View):

    def get(self, request):
        from geonode.geoserver import ows
        out = {'success': True}
        data = []
        out['data'] = data
        # WMS
        _raw_url = ows._wms_get_capabilities()
        _url = urlsplit(_raw_url)
        headers, access_token = get_headers(request, _url, _raw_url)
        if access_token:
            _j = '&' if _url.query else '?'
            _raw_url = _j.join([_raw_url, f'access_token={access_token}'])
        data.append({'url': _raw_url, 'type': 'OGC:WMS'})

        # WCS
        _raw_url = ows._wcs_get_capabilities()
        _url = urlsplit(_raw_url)
        headers, access_token = get_headers(request, _url, _raw_url)
        if access_token:
            _j = '&' if _url.query else '?'
            _raw_url = _j.join([_raw_url, f'access_token={access_token}'])
        data.append({'url': _raw_url, 'type': 'OGC:WCS'})

        # WFS
        _raw_url = ows._wfs_get_capabilities()
        _url = urlsplit(_raw_url)
        headers, access_token = get_headers(request, _url, _raw_url)
        if access_token:
            _j = '&' if _url.query else '?'
            _raw_url = _j.join([_raw_url, f'access_token={access_token}'])
        data.append({'url': _raw_url, 'type': 'OGC:WFS'})

        # catalogue from configuration
        for catname, catconf in settings.CATALOGUE.items():
            # CSW
            _raw_url = catconf['URL']
            _url = urlsplit(_raw_url)
            headers, access_token = get_headers(request, _url, _raw_url)
            if access_token:
                _j = '&' if _url.query else '?'
                _raw_url = _j.join([_raw_url, f'access_token={access_token}'])
            data.append({'url': _raw_url, 'type': 'OGC:CSW'})

        # main site url
        data.append({'url': settings.SITEURL, 'type': 'WWW:LINK'})
        return json_response(out)


_hoppish = {
    'connection', 'keep-alive', 'proxy-authenticate',
    'proxy-authorization', 'te', 'trailers', 'transfer-encoding',
    'upgrade', 'content-length', 'content-encoding'
}.__contains__


def is_hop_by_hop(header_name):
    """Return true if 'header_name' is an HTTP/1.1 "Hop-by-Hop" header"""
    return _hoppish(header_name.lower())


def fetch_response_headers(response, response_headers):
    if response_headers:
        for _header in response_headers:
            if not is_hop_by_hop(_header):
                if hasattr(response, 'headers') and _header.lower() not in [_k.lower() for _k in response.headers.keys()]:
                    response.headers[_header] = response_headers.get(_header)
                elif hasattr(response, '_headers') and _header.lower() not in [_k.lower() for _k in response._headers.keys()]:
                    response._headers[_header] = (_header, response_headers.get(_header))
    return response
