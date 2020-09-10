"""
Script for load-testing the license-manager service

requirements you must pip install these before using: requests

To source env variables, consider `source scripts/loadtest.env.development`.
"""

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from multiprocessing import Pool
from contextlib import contextmanager
import json
import os
from pprint import pprint
import random
import sys
import time
import uuid

import requests

# The local defaults should all "just work" against devstack.

CACHE_FILENAME = os.environ.get('CACHE_FILENAME', 'loadtest-cache.json')

# used to fetch a JWT for the license_manager_worker
OAUTH2_CLIENT_ID = os.environ.get('OAUTH2_CLIENT_ID', 'license_manager-backend-service-key')
OAUTH2_CLIENT_SECRET = os.environ.get('OAUTH2_CLIENT_SECRET', 'license_manager-backend-service-secret')

# where to get a JWT, can change the env var to set this to the stage environment
LMS_BASE_URL = os.environ.get('LMS_BASE_URL', 'http://localhost:18000')
ACCESS_TOKEN_ENDPOINT = LMS_BASE_URL + '/oauth2/access_token/'

# the license-manager root URL, you can change the env var to set this to stage
LICENSE_MANAGER_BASE_URL = os.environ.get('LICENSE_MANAGER_BASE_URL', 'http://localhost:18170')

LEARNER_NAME_TEMPLATE = 'Subscription Learner {}'
LEARNER_USERNAME_TEMPLATE = 'subsc-learner-{}'
LEARNER_EMAIL_TEMPLATE = '{}@example.com'.format(LEARNER_USERNAME_TEMPLATE)
LEARNER_PASSWORD_TEMPLATE = 'random-password-{}'

TIME_MEASUREMENTS = {}

# TODO Make these be a command line args
NUM_USERS = int(os.environ.get('NUM_USERS', 10))
SUBSCRIPTION_PLAN_UUID = os.environ.get('SUBSCRIPTION_PLAN_UUID', '9d81c494-fb47-46a1-a596-58e5bca037e1')

def _now():
    return datetime.utcnow().timestamp()


def _later(**timedelta_kwargs):
    defaults = {'hours': 1}
    later = datetime.utcnow() + timedelta(**timedelta_kwargs or defaults)
    return later.timestamp()


def _random_hex_string(a=0, b=1e6):
    """
    Return a random hex string, default range between 0 and 1 million.
    """
    return hex(random.randint(a, b)).lstrip('0x')


def _random_password(**kwargs):
    return LEARNER_PASSWORD_TEMPLATE.format(_random_hex_string(**kwargs))


def _random_user_data():
    hex_str = _random_hex_string()
    return {
        'email': LEARNER_EMAIL_TEMPLATE.format(hex_str),
        'name': LEARNER_NAME_TEMPLATE.format(hex_str),
        'username': LEARNER_USERNAME_TEMPLATE.format(hex_str),
        'password': _random_password(),
        'country': 'US',
        'honor_code': 'true',
    }


class Cache:
    class Keys:
        REQUEST_JWT = '__request_jwt'
        REGISTERED_USERS = '__registered_users'
        USER_JWT_TEMPLATE = '__jwt_for_user_{}'
        ALL_SUBSCRIPTION_PLANS = '__subscription_plans'

    def __init__(self):
        self._data = {}
        self._expiry = {}

    def get(self, key, default=None):
        value = self._data.get(key)
        if not value:
            return None or default

        expiry = self._expiry.get(key)
        if expiry and (_now() > expiry):
            self.evict(key)
            return None or default

        return value or default

    def set(self, key, value, expiry=None):
        self._data[key] = value
        self._expiry[key] = expiry

    def evict(self, key):
        self._data.pop(key, None)
        self._expiry.pop(key, None)

    def to_dict(self):
        return {
            '_data': self._data,
            '_expiry': self._expiry,
        }

    @classmethod
    def from_dict(cls, _dict):
        instance = cls()
        instance._data = _dict['_data']
        instance._expiry = _dict['_expiry']
        return instance

    def current_request_jwt(self):
        return self.get(self.Keys.REQUEST_JWT)

    def set_current_request_jwt(self, jwt):
        self.set(self.Keys.REQUEST_JWT, jwt, expiry=_later())

    def clear_current_request_jwt(self):
        self.evict(self.Keys.REQUEST_JWT)

    def registered_users(self):
        return self.get(self.Keys.REGISTERED_USERS, dict())

    def add_registered_user(self, user_data):
        users = self.registered_users()
        if user_data['email'] not in users:
            users[user_data['email']] = user_data
        self.set(self.Keys.REGISTERED_USERS, users)

    def set_jwt_for_email(self, email, jwt):
        key = self.Keys.USER_JWT_TEMPLATE.format(email)
        self.set(key, jwt, expiry=_later())

    def get_jwt_for_email(self, email):
        key = self.Keys.USER_JWT_TEMPLATE.format(email)
        return self.get(key)

    def subscription_plans(self):
        return self.get(self.Keys.ALL_SUBSCRIPTION_PLANS, dict())

    def add_subscription_plan(self, subsc_data):
        all_plans = self.subscription_plans()
        all_plans[subsc_data['uuid']] = subsc_data
        self.set(self.Keys.ALL_SUBSCRIPTION_PLANS, all_plans)

    def set_license_for_email(self, email_address, license_data):
        users = self.registered_users()
        if email_address not in users:
            return
        user_data = users[email_address]
        user_data['license'] = license_data
        users[email_address] = user_data
        self.set(self.Keys.REGISTERED_USERS, users)

    def get_license_uuids(self):
        users = self.registered_users()
        license_uuids = []
        for user_data in users.values():
            license_data = user_data.get('license', {})
            if license_data:
                license_uuids.append(license_data.get('uuid'))
        return license_uuids

    @property
    def license_uuids_by_status(self):
        license_data_by_status = defaultdict(dict)
        for email, user_data in self.registered_users().items():
            license_data = user_data.get('license', {})
            status = license_data.get('status', 'unassigned')
            license_data_by_status[status][email] = license_data
        return license_data_by_status

    def print_user_license_data(self):
        print('Currently cached user count by status:')
        pprint({
            status: len(user_license_data)
            for status, user_license_data
            in self.license_uuids_by_status.items()
        })


CACHE = Cache()


def _dump_cache():
    with open(CACHE_FILENAME, 'w') as file_out:
        file_out.write(json.dumps(CACHE.to_dict()))


def _load_cache():
    global CACHE
    try:
        with open(CACHE_FILENAME, 'r') as file_in:
            json_data = json.loads(file_in.read())
            CACHE = Cache.from_dict(json_data)
    except:
        CACHE = Cache()

    # for the sake of convenience, try to get the cached request JWT
    # so that it's evicted if already expired
    CACHE.current_request_jwt()


@contextmanager
def _load_cache_and_dump_when_done():
    try:
        _load_cache()
        yield
    finally:
        _dump_cache()


def _get_admin_jwt():
    if CACHE.current_request_jwt():
        return CACHE.current_request_jwt()

    payload = {
        'client_id': OAUTH2_CLIENT_ID,
        'client_secret': OAUTH2_CLIENT_SECRET,
        'grant_type': 'client_credentials',
        'token_type': 'jwt',
    }
    response = requests.post(
        ACCESS_TOKEN_ENDPOINT,
        data=payload,
    )
    jwt_token = response.json().get('access_token')
    CACHE.set_current_request_jwt(jwt_token)
    return jwt_token


def _get_jwt_from_response_and_add_to_cache(response, user_data=None):
    # The cookie name is prefixed with "stage" in the staging environment
    prefix = ''
    if 'stage' in CACHE_FILENAME.lower():
        prefix = 'stage-'

    jwt_header = response.cookies.get(prefix + 'edx-jwt-cookie-header-payload')
    jwt_signature = response.cookies.get(prefix + 'edx-jwt-cookie-signature')
    jwt = jwt_header + '.' + jwt_signature
    CACHE.add_registered_user(user_data)
    CACHE.set_jwt_for_email(user_data['email'], jwt)
    return jwt


def _register_user(**kwargs):
    url = LMS_BASE_URL + '/user_api/v2/account/registration/'
    user_data = _random_user_data()
    response = requests.post(url, data=user_data)
    if response.status_code == 200:
        # print('Successfully created new account for {}'.format(user_data['email']))
        jwt = _get_jwt_from_response_and_add_to_cache(response, user_data)
        return response, jwt
    else:
        print('Failed to create new account for {}!'.format(user_data['email']))
        raise
    return response, None


def register_users():
    for _ in range(NUM_USERS):
        _register_user()
    print('Successfully created {} new users.\n'.format(NUM_USERS))


def _get_learner_jwt(email):
    jwt = CACHE.get_jwt_for_email(email)
    if not jwt:
        password = CACHE.registered_users().get(email, {}).get('password')
        if not password:
            print('Cannot get JWT for {}, no password'.format(email))
            return

        # this will actually put a new JWT in the cache for the given email
        _login_session(email, password)
        jwt = CACHE.get_jwt_for_email(email)
    return jwt


def _login_session(email, password):
    request_headers = {
        'use-jwt-cookie': 'true',
        'X-CSRFToken': 'b2m2Szrpqk5mcGxdImCL0nvqq7hTJJWIUQvriT6NnphlNVtEGz0xwaB6JfiXkkNj',
    }
    request_cookies = {
        'csrftoken': 'b2m2Szrpqk5mcGxdImCL0nvqq7hTJJWIUQvriT6NnphlNVtEGz0xwaB6JfiXkkNj',
    }
    user_data = {
        'email': email,
        'password': password,
    }
    url = LMS_BASE_URL + '/user_api/v1/account/login_session/'
    response = requests.post(url, headers=request_headers, cookies=request_cookies, data=user_data)
    if response.status_code == 200:
        print('Successfully logged in {}'.format(user_data['email']))
        jwt = _get_jwt_from_response_and_add_to_cache(response, user_data)
        return response, jwt
    return response, None


def _make_request(url, delay_seconds=None, jwt=None, request_method=requests.get, **kwargs):
    """
    Makes an authenticated request to a given URL
    Will use the admin Authorization token by default.
    """
    headers = {
        "Authorization": "JWT {}".format(jwt or _get_admin_jwt()),
    }
    response = request_method(
        url,
        headers=headers,
        **kwargs
    )

    if delay_seconds:
        time.sleep(delay_seconds)

    return response


def fetch_all_subscription_plans(*args, **kwargs):
    """
    Fetch data on all subscription plans and cache it.
    """
    def _process_results(data):
        for subscription in data:
            print('Updating subscription plan {} ({}) in cache.'.format(subscription['uuid'], subscription['title']))
            CACHE.add_subscription_plan(subscription)

    url = LICENSE_MANAGER_BASE_URL + '/api/v1/subscriptions/'

    while url:
        response = _make_request(url)
        response_data = response.json()
        _process_results(response_data['results'])
        url = response_data['next']


def fetch_one_subscription_plan(plan_uuid, user_email=None):
    if user_email:
        subscription_data = CACHE.subscription_plans().get(plan_uuid, {})
        enterprise_customer_uuid = subscription_data.get('enterprise_customer_uuid')
        url = LICENSE_MANAGER_BASE_URL + '/api/v1/learner-subscriptions/?enterprise_customer_uuid={}'.format(enterprise_customer_uuid)
    else:
        url = LICENSE_MANAGER_BASE_URL + '/api/v1/subscriptions/{}/'.format(plan_uuid)

    if not user_email:
        jwt = _get_admin_jwt()
    else:
        jwt = _get_learner_jwt(user_email)

    response = _make_request(url, jwt=jwt)
    if response.status_code != 200:
        print("Encountered an error while fetching a subscription plan: {}".format(response))
    return response.json()


def assign_licenses(plan_uuid, user_emails):
    url = LICENSE_MANAGER_BASE_URL + '/api/v1/subscriptions/{}/licenses/assign/'.format(plan_uuid)
    print('Attempting to assign licenses for {} emails...'.format(len(user_emails)))
    data = {
        'user_emails': user_emails,
    }
    # It's important to use json=data here, and not data=data
    # "Using the json parameter in the request will change the Content-Type in the header to application/json."
    # https://requests.readthedocs.io/en/master/user/quickstart/#more-complicated-post-requests
    response = _make_request(url, json=data, request_method=requests.post)
    response_data = response.json()
    print('Result of license assignment: {}\n'.format(response_data))
    return response_data


def remind(plan_uuid, user_email=None):
    url = LICENSE_MANAGER_BASE_URL + '/api/v1/subscriptions/{}/licenses/remind/'.format(plan_uuid)
    data = {
        'user_email': user_email,
    }
    # It's important to use json=data here, and not data=data
    # "Using the json parameter in the request will change the Content-Type in the header to application/json."
    # https://requests.readthedocs.io/en/master/user/quickstart/#more-complicated-post-requests
    response = _make_request(url, json=data, request_method=requests.post)
    if response.status_code != 200:
        print("Encountered an error while reminding: {}".format(response))
    return response


def remind_all(plan_uuid):
    url = LICENSE_MANAGER_BASE_URL + '/api/v1/subscriptions/{}/licenses/remind-all/'.format(plan_uuid)
    response = _make_request(url, request_method=requests.post)
    if response.status_code != 200:
        print("Encountered an error while reminding all learners: {}".format(response))
    return response


def fetch_licenses(plan_uuid, status=None):
    """
    Fetch all licenses for a given subscription plan.  Optionally filter by a license status.
    Will associate licenses with cached users as appropriate.
    """
    def _process_results(data):
        for license_data in data:
            user_email = license_data.pop('user_email')
            if user_email:
                CACHE.set_license_for_email(user_email, license_data)

    url = LICENSE_MANAGER_BASE_URL + '/api/v1/subscriptions/{}/licenses/?page_size=100'.format(plan_uuid)
    if status:
        url += '&status={}'.format(status)

    while url:
        response = _make_request(url)
        response_data = response.json()
        _process_results(response_data['results'])
        url = response_data['next']

    # Return how many licenses exist for this subscription (optionally with the given status)
    return response_data['count']


def fetch_individual_licenses(plan_uuid, license_uuids):
    for license_uuid in license_uuids:
        url = LICENSE_MANAGER_BASE_URL + '/api/v1/subscriptions/{}/licenses/{}/'.format(plan_uuid, license_uuid)
        response = _make_request(url, request_method=requests.get)
        if response.status_code != 200:
            print("Encountered an error while fetching an individual license: {}".format(response))


def fetch_learner_license(plan_uuid, user_email):
    url = LICENSE_MANAGER_BASE_URL + '/api/v1/subscriptions/{}/license'.format(plan_uuid)
    response = _make_request(url, jwt=_get_learner_jwt(user_email), request_method=requests.get)
    if response.status_code != 200:
        print("Encountered an error while a learner tried fetching their own license: {}".format(response))
    return response


def fetch_license_overview(plan_uuid):
    url = LICENSE_MANAGER_BASE_URL + '/api/v1/subscriptions/{}/licenses/overview/'.format(plan_uuid)
    response = _make_request(url, request_method=requests.get)
    if response.status_code != 200:
        print("Encountered an error while fetching the license overview: {}".format(response))
    return response


def revoke_license(plan_uuid, user_email):
    url = LICENSE_MANAGER_BASE_URL + '/api/v1/subscriptions/{}/licenses/revoke/'.format(plan_uuid)
    data = {
        'user_email': user_email,
    }
    # It's important to use json=data here, and not data=data
    # "Using the json parameter in the request will change the Content-Type in the header to application/json."
    # https://requests.readthedocs.io/en/master/user/quickstart/#more-complicated-post-requests
    response = _make_request(url, jwt=_get_admin_jwt(), json=data, request_method=requests.post)
    if response.status_code != 204:
        print("Encountered an error while fetching revoking a license: {}".format(response))
    return response


def activate_license(user_email):
    user_data = CACHE.registered_users().get(user_email)
    if not user_data:
        print('No user record for {}'.format(user_email))

    license_data = user_data.get('license')
    if not license_data:
        print('No license data for user {}'.format(user_email))
        return

    if license_data.get('status') == 'activated':
        print('License for user {} already activated'.format(user_email))
        return

    activation_key = license_data.get('activation_key')
    if not activation_key:
        print('No activation_key for licensed-user {}'.format(user_email))
        return

    url = LICENSE_MANAGER_BASE_URL + '/api/v1/license-activation?activation_key={}'.format(activation_key)
    response = _make_request(
        url,
        request_method=requests.post,
        jwt=CACHE.get_jwt_for_email(user_email),
    )
    if response.status_code >= 400:
        print('Error activating license: {}'.format(response))
    else:
        license_data['status'] = 'activated'
        print('License successfully activated for {}'.format(user_email))
    return response

def main():
    print('Using cache filename: {}'.format(CACHE_FILENAME))
    with _load_cache_and_dump_when_done():
        CACHE.print_user_license_data()
        # Forces us to always fetch a fresh JWT for the worker/admin user.
        CACHE.clear_current_request_jwt()
        _test_register()

        # Tests for the license-manager endpoints
        _enterprise_subscription_requests()
        # _learner_subscription_requests()

        # Test revoking licenses
        # _test_revoke_licenses()

        pretty_timings = {
            key: '{} ms'.format(round(value, 3) * 1000)
            for key, value in TIME_MEASUREMENTS.items()
        }
        print('***Results***')
        for key, value in pretty_timings.items():
            print('{}: {}'.format(key, value))

        # TODO Test this script out for larger magnitudes of licenses on subscriptions
        # TODO see if creating a subscription + unassigned licenses on it can be done without manually doing so in django admin


## Functions to test that things work ##
def _enterprise_subscription_requests():
    # As an admin, test assigning licenses to users
    _test_assign_licenses()

    # # As an admin, test reminding learners about their licenses
    # _test_remind()

    # # As an admin, test loading subscriptions
    # _test_load_subscriptions()

    # # As an admin, test loading licenses
    # _test_load_licenses()


def _learner_subscription_requests():
    activate_accumulated_time = 0
    load_subscription_accumulated_time = 0
    load_license_accumulated_time = 0

    for user_email, user_data in CACHE.registered_users().items():
        if user_data.get('license'):
            # get a new JWT for the user, this is important because
            # the activation endpoint matches the JWT email against the assigned license
            _login_session(user_email, user_data['password'])

            # As a learner test how long it takes to activate licenses
            start = time.time()
            activate_license(user_email)
            activate_accumulated_time += time.time() - start

            # As each learner test how long it takes on average to load my subscription plan info
            start = time.time()
            fetch_one_subscription_plan(
                SUBSCRIPTION_PLAN_UUID,
                user_email=user_email,
            )
            load_subscription_accumulated_time += time.time() - start

            # As each learner test how long it takes on average to load my license info
            start = time.time()
            fetch_learner_license(
                SUBSCRIPTION_PLAN_UUID,
                user_email=user_email
            )
            load_license_accumulated_time += time.time() - start

    TIME_MEASUREMENTS['Average Activate License Time'] = activate_accumulated_time / NUM_USERS
    TIME_MEASUREMENTS['Average Learner Load Subscription Time'] = load_subscription_accumulated_time / NUM_USERS
    TIME_MEASUREMENTS['Average Learner Load License Time'] = load_license_accumulated_time / NUM_USERS


def _test_register():
    """
    Run this to generate NUM_USERS number of new users.
    """
    register_users()


def _test_login():
    """
    Run this to verify that we have retained data when registering users
    and can log each of them in.
    """
    for user_email, user_data in CACHE.registered_users().items():
        password = user_data['password']
        _login_session(user_email, password)


def _test_assign_licenses():
    unassigned_emails = list(CACHE.license_uuids_by_status.get('unassigned').keys())

    start = time.time()
    assign_licenses(SUBSCRIPTION_PLAN_UUID, unassigned_emails)
    TIME_MEASUREMENTS['Assign Licenses Time (N = {})'.format(len(unassigned_emails))] = time.time() - start

    # assure that cached user-license data is updated
    start = time.time()
    license_count = fetch_licenses(SUBSCRIPTION_PLAN_UUID, status='assigned')
    TIME_MEASUREMENTS['Fetch Assigned Licenses Time (N = {})'.format(license_count)] = time.time() - start



def _test_remind():
    plan_uuid = SUBSCRIPTION_PLAN_UUID
    user_emails = list(CACHE.registered_users().keys())
    # Test reminding users to activate their license
    start = time.time()
    for user_email in user_emails:
        remind(plan_uuid=plan_uuid, user_email=user_email)
    num_emails = len(user_emails)
    TIME_MEASUREMENTS['Average Remind Single User Time (N = {})'.format(num_emails)] = (time.time() - start) / num_emails

    # Test reminding all users to activate their license
    start = time.time()
    remind_all(plan_uuid=plan_uuid)
    TIME_MEASUREMENTS['Remind All Users Time'] = time.time() - start


def _test_load_subscriptions():
    # Test loading all subscriptions
    start = time.time()
    fetch_all_subscription_plans()
    TIME_MEASUREMENTS['Load All Subscriptions Time'] = time.time() - start

    # Test loading individual subscription info
    start = time.time()
    fetch_one_subscription_plan(SUBSCRIPTION_PLAN_UUID)
    TIME_MEASUREMENTS['Average Load Single Subscription Time'] = time.time() - start


def _test_load_licenses():
    # Test fetching all licenses
    start = time.time()
    license_count = fetch_licenses(SUBSCRIPTION_PLAN_UUID)
    TIME_MEASUREMENTS['Fetch All Licenses Time (N = {})'.format(license_count)] = time.time() - start

    # Fetch and cache the licenses in the subscription
    license_uuids = CACHE.get_license_uuids()
    start = time.time()
    fetch_individual_licenses(SUBSCRIPTION_PLAN_UUID, license_uuids)
    num_licenses = len(license_uuids)
    avg_license_time = (time.time() - start) / num_licenses
    TIME_MEASUREMENTS['Average Time Fetching Individual License (N = {})'.format(num_licenses)] = avg_license_time

    # Test fetching the license overview for the subscription
    start = time.time()
    fetch_license_overview(SUBSCRIPTION_PLAN_UUID)
    TIME_MEASUREMENTS['Fetch Licenses Overview Time'] = time.time() - start


def _test_revoke_licenses():
    # Test revoking all licenses
    user_emails = list(CACHE.registered_users().keys())
    # Test reminding users to activate their license
    start = time.time()
    for user_email in user_emails:
        revoke_license(SUBSCRIPTION_PLAN_UUID, user_email)
    TIME_MEASUREMENTS['Average Revoke License Time'] = (time.time() - start) / NUM_USERS

## End all testing functions ###


if __name__ == '__main__':
    main()
