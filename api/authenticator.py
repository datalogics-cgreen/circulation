from nose.tools import set_trace
from config import (
    Configuration,
    CannotLoadConfiguration,
)
from core.util.problem_detail import (
    ProblemDetail,
    json as pd_json,
)
from core.util.opds_authentication_document import OPDSAuthenticationDocument
from problem_details import *

import urlparse
import urllib
import uuid
import json
import jwt
import flask
from flask import (
    Response,
    redirect,
    url_for,
)
from werkzeug.datastructures import Headers
from flask.ext.babel import lazy_gettext as _
import importlib


class Authenticator(object):

    BASIC_AUTH = 'basic_auth'
    OAUTH = 'oauth'

    @classmethod
    def initialize(cls, _db, test=False):
        if test:
            from millenium_patron import (
                DummyMilleniumPatronAPI,
            )
            return cls(DummyMilleniumPatronAPI(), [])

        providers = Configuration.policy("authentication")
        if not providers:
            raise CannotLoadConfiguration(
                "No authentication policy given."
            )
        if isinstance(providers, basestring):
            providers = [providers]
        basic_auth_provider = None
        oauth_providers = []

        for provider_string in providers:
            provider_module = importlib.import_module(provider_string)
            provider_class = getattr(provider_module, "AuthenticationAPI")
            if provider_class.TYPE == Authenticator.BASIC_AUTH:
                if basic_auth_provider != None:
                    raise CannotLoadConfiguration(
                        "Two basic auth providers configured"
                    )
                basic_auth_provider = provider_class.from_config()
            elif provider_class.TYPE == Authenticator.OAUTH:
                oauth_providers.append(provider_class.from_config())
            else:
                raise CannotLoadConfiguration(
                    "Unrecognized authentication provider: %s" % provider
                )

        if not basic_auth_provider and not oauth_providers:
            raise CannotLoadConfiguration(
                "No authentication provider configured"
            )
        return cls(basic_auth_provider, oauth_providers)

    def __init__(self, basic_auth_provider=None, oauth_providers=None):
        self.basic_auth_provider = basic_auth_provider
        self.oauth_providers = oauth_providers or []
        self.secret_key = Configuration.get(Configuration.SECRET_KEY)

    def server_side_validation(self, identifier, password):
        if not hasattr(self, 'identifier_re'):
            self.identifier_re = Configuration.policy(
                Configuration.IDENTIFIER_REGULAR_EXPRESSION,
                default=Configuration.DEFAULT_IDENTIFIER_REGULAR_EXPRESSION)
        if not hasattr(self, 'password_re'):
            self.password_re = Configuration.policy(
                Configuration.PASSWORD_REGULAR_EXPRESSION,
                default=Configuration.DEFAULT_PASSWORD_REGULAR_EXPRESSION)

        valid = True
        if self.identifier_re:
            valid = valid and identifier and (self.identifier_re.match(identifier) is not None)
        if self.password_re:
            valid = valid and password and (self.password_re.match(password) is not None)
        return valid

    def decode_token(self, token):
        """Extract auth provider name and access token from JSON web token."""
        decoded = jwt.decode(token, self.secret_key, algorithms=['HS256'])
        provider_name = decoded['iss']
        token = decoded['token']
        return (provider_name, token)

    def create_token(self, provider_name, provider_token):
        """Create a JSON web token with the provider name and access token."""
        payload = dict(
            token=provider_token,
            # I'm not sure this is the correct way to use an
            # Issuer claim (https://tools.ietf.org/html/rfc7519#section-4.1.1).
            # Maybe we should use something custom instead.
            iss=provider_name,
        )
        return jwt.encode(payload, self.secret_key, algorithm='HS256')

    def get_credential_from_header(self, header):
        if self.basic_auth_provider and 'password' in header:
            return header['password']
        return None

    def authenticated_patron(self, _db, header):
        if self.basic_auth_provider and 'password' in header:
            return self.basic_auth_provider.authenticated_patron(_db, header)
        elif self.oauth_providers and 'bearer' in header.lower():
            simplified_token = header.split(' ')[1]
            provider_name, provider_token = self.decode_token(simplified_token)
            for provider in self.oauth_providers:
                if provider_name == provider.NAME:
                    return provider.authenticated_patron(_db, provider_token)
        return None

    def _redirect_with_error(self, redirect_uri, pd):
        problem_detail_json = pd_json(pd.uri, pd.status_code, pd.title, pd.detail, pd.instance, pd.debug_message)
        params = dict(error=problem_detail_json)
        return redirect(redirect_uri + "#" + urllib.urlencode(params))

    def oauth_authenticate(self, params):
        redirect_uri = params.get('redirect_uri') or ""
        if self.oauth_providers and params.get('provider'):
            for provider in self.oauth_providers:
                if params.get('provider') == provider.NAME:
                    state = dict(provider=provider.NAME, redirect_uri=redirect_uri)
                    return redirect(provider.external_authenticate_url(json.dumps(state)))
        return self._redirect_with_error(
            redirect_uri,
            UNKNOWN_OAUTH_PROVIDER.detailed(UNKNOWN_OAUTH_PROVIDER.detail + _(" The known providers are: %s") % ", ".join([p.NAME for p in self.oauth_providers]))
        )

    def oauth_callback(self, _db, params):
        if self.oauth_providers and params.get('code') and params.get('state'):
            state = json.loads(params.get('state'))
            client_redirect_uri = state.get('redirect_uri') or ""
            for provider in self.oauth_providers:
                if state.get('provider') == provider.NAME:
                    provider_token, patron_info = provider.oauth_callback(_db, params)
                    
                    if isinstance(provider_token, ProblemDetail):
                        return self._redirect_with_error(client_redirect_uri, provider_token)

                    # Create an access token for our app that includes the provider
                    # as well as the provider's token.
                    simplified_token = self.create_token(provider.NAME, provider_token)

                    params = dict(access_token=simplified_token, patron_info=json.dumps(patron_info))
                    return redirect(client_redirect_uri + "#" + urllib.urlencode(params))
            return self._redirect_with_error(
                client_redirect_uri,
                UNKNOWN_OAUTH_PROVIDER.detailed(UNKNOWN_OAUTH_PROVIDER.detail + _(" The known providers are: %s") % ", ".join([p.NAME for p in self.oauth_providers]))
            )
        return INVALID_OAUTH_CALLBACK_PARAMETERS

    def patron_info(self, header):
        if self.basic_auth_provider and 'password' in header:
            return self.basic_auth_provider.patron_info(header.get('username'))
        elif self.oauth_providers and 'bearer' in header.lower():
            simplified_token = header.split(' ')[1]
            provider_name, provider_token = self.decode_token(simplified_token)
            for provider in self.oauth_providers:
                if provider_name == provider.NAME:
                    return provider.patron_info(provider_token)
        return {}
            

    def create_authentication_document(self):
        """Create the OPDS authentication document to be used when
        there's a 401 error.
        """
        base_opds_document = Configuration.base_opds_authentication_document()

        circulation_manager_url = Configuration.integration_url(
            Configuration.CIRCULATION_MANAGER_INTEGRATION, required=True)
        scheme, netloc, path, parameters, query, fragment = (
            urlparse.urlparse(circulation_manager_url))

        opds_id = str(uuid.uuid3(uuid.NAMESPACE_DNS, str(netloc)))

        providers = []
        if self.basic_auth_provider:
            providers.append(self.basic_auth_provider)
        for provider in self.oauth_providers:
            providers.append(provider)

        links = {}
        for rel, value in (
                ("terms-of-service", Configuration.terms_of_service_url()),
                ("privacy-policy", Configuration.privacy_policy_url()),
                ("copyright", Configuration.acknowledgements_url()),
                ("about", Configuration.about_url()),
        ):
            if value:
                links[rel] = dict(href=value, type="text/html")

        doc = OPDSAuthenticationDocument.fill_in(
            base_opds_document, providers,
            name=unicode(_("Library")), id=opds_id, links=links,
        )
        return json.dumps(doc)

    def create_authentication_headers(self):
        """Create the HTTP headers to return with the OPDS
        authentication document."""
        headers = Headers()
        headers.add('Content-Type', OPDSAuthenticationDocument.MEDIA_TYPE)
        # if requested from a web client, don't include WWW-Authenticate header,
        # which forces the default browser authentication prompt
        if self.basic_auth_provider and not flask.request.headers.get("X-Requested-With") == "XMLHttpRequest":
            headers.add('WWW-Authenticate', self.basic_auth_provider.AUTHENTICATION_HEADER)

        # TODO: We're leaving out headers for other providers to avoid breaking iOS
        # clients that don't support multiple auth headers. It's not clear what
        # the header for an oauth provider should look like. This means that there's
        # no auth header for app without a basic auth provider, but we don't have
        # any apps like that yet.

        return headers

class BasicAuthAuthenticator(Authenticator):

    TYPE = Authenticator.BASIC_AUTH
    METHOD = "http://opds-spec.org/auth/basic"
    NAME = _("Library Barcode")
    URI = "http://librarysimplified.org/terms/auth/library-barcode"

    AUTHENTICATION_HEADER = 'Basic realm="%s"' % _("Library card")

    LOGIN_LABEL = _("Barcode")
    PASSWORD_LABEL = _("PIN")

    def testing_patron(self, _db):
        """Return a real Patron object reserved for testing purposes.

        :return: A 2-tuple (Patron, password)
        """
        if self.test_username is None or self.test_password is None:
            return None, None
        header = dict(username=self.test_username, password=self.test_password)
        return self.authenticated_patron(_db, header), self.test_password

    def create_authentication_provider_document(self):
        method_doc = dict(labels=dict(login=unicode(self.LOGIN_LABEL), password=unicode(self.PASSWORD_LABEL)))
        methods = {}
        methods[self.METHOD] = method_doc
        return dict(name=unicode(self.NAME), methods=methods)

class OAuthAuthenticator(Authenticator):

    TYPE = Authenticator.OAUTH
    # Subclass must define NAME, URI, and METHOD

    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret

    @classmethod
    def from_config(cls):
        config = Configuration.integration(cls.NAME, required=True)
        client_id = config.get(Configuration.OAUTH_CLIENT_ID)
        client_secret = config.get(Configuration.OAUTH_CLIENT_SECRET)
        return cls(client_id, client_secret)

    def external_authenticate_url(self, client_redirect_uri):
        raise NotImplementedError()

    def authenticated_patron(self, _db, token):
        raise NotImplementedError()

    def oauth_callback(self, _db, params):
        raise NotImplementedError()

    def patron_info(self, identifier):
        return {}

    def _internal_authenticate_url(self):
        return url_for('oauth_authenticate', _external=True, provider=self.NAME)

    def create_authentication_provider_document(self):
        method_doc = dict(links=dict(authenticate=self._internal_authenticate_url()))
        methods = {}
        methods[self.METHOD] = method_doc
        return dict(name=self.NAME, methods=methods)
