"""
@author Team 42, Chengdu, China, Qifan Deng, 1077479
"""
import hashlib
import logging
import traceback
import tweepy
import os

from math import ceil
from signal import SIGUSR1
from threading import Lock
from time import sleep, time

from utils.config import Config
from utils.logger import get_logger


class Crawler:

    def __init__(self, log_level):
        """
        A crawler that get result from twitter api
        :param log_level: log printing level
        """
        # create attributes
        self.logger = get_logger('Crawler', log_level)
        self.config = Config(log_level)
        self.lock_friends = Lock()
        self.lock_user_timeline = Lock()
        self.lock_rate_limits = Lock()
        self.lock_followers = Lock()
        self.lock_lookup_users = Lock()
        self.id = None
        self.api_keys = {}
        for idx, credential in enumerate(self.config.twitter):
            # generate api key hashes map
            api_key = credential['api_key']
            api_secret_key = credential['api_secret_key']
            access_token = credential['access_token']
            access_token_secret = credential['access_token_secret']
            stream_area = credential['stream_area']
            stream_bbox = credential['stream_bbox']
            api_key_hash = self.hash(api_key)
            self.api_keys[api_key_hash] = (api_key, api_secret_key, access_token, access_token_secret,
                                           stream_bbox, stream_area)

        self.api = None
        self.rate_limits = None
        self.rate_limits_updated_at = 0

        self.access_user_timeline = 0
        self.access_friends = 0
        self.access_followers = 0
        self.access_lookup_users = 0
        self.stream_bbox = None
        self.stream_area = None

    def init(self, hash_, id_):
        """
        Authorise the credential
        :param hash_: api key hash
        :param id_: unique worker id
        """
        self.id = id_
        (api_key, api_secret_key, access_token, access_token_secret, stream_bbox, stream_area) = self.api_keys[hash_]
        auth = tweepy.OAuthHandler(api_key, api_secret_key)
        auth.set_access_token(access_token, access_token_secret)
        self.api = tweepy.API(auth)
        self.update_rate_limit_status()
        self.stream_bbox = stream_bbox
        self.stream_area = stream_area
        self.logger.info("Init crawler")

    def stream_filter(self, id_, res_queue, log_level):
        """
        Stream listener with filter
        :param id_: unique worker id
        :param res_queue: stream statuses results queue
        :param log_level: log printing level
        """
        try:
            # create a stream listener
            stream_listener = StreamListener(id_, res_queue, log_level)
            # create a stream object
            stream_ = tweepy.Stream(auth=self.api.auth, listener=stream_listener)
            self.logger.info("[{}] Stream filter {}: {}".format(id_, self.stream_area, self.stream_bbox))
            # filter and lister the statuses stream
            # blocking method
            stream_.filter(languages=['en'], locations=self.stream_bbox)
        except Exception as e:
            self.logger.warning(e)

    def update_rate_limit_status(self, err_count=0):
        """
        update rate limit
        :param err_count: err count used to achieve using an exponential back-off pattern
        """
        if err_count > self.config.max_network_err:
            self.logger.debug("[*] Err {} times, exit".format(self.config.max_network_err))
            os._exit(0)

        # lock rate limit
        self.lock_rate_limits.acquire()
        # sleep if it was accessed in the previous 5 seconds
        self.sleep(int(time()) - self.rate_limits_updated_at, 5)
        # update its accessing time
        self.rate_limits_updated_at = int(time())
        self.lock_rate_limits.release()
        try:
            # get the rate limits from twitter api
            self.rate_limits = self.api.rate_limit_status()
            self.logger.debug("[{}] Updated rate limit status.".format(self.id))
        except tweepy.TweepError:
            if self.rate_limits is None:
                self.logger.warning("Get rate limit occurs rate limit error, sleep 15 minutes and try again.")
                sleep(15 * 60)
            else:
                # get the rate limit reset time
                reset = self.rate_limits['resources']['application']['/application/rate_limit_status']['reset']
                now_timestamp = int(time())
                # sleep to the reset time
                to_sleep = abs(ceil((now_timestamp - reset) / 900) * 900)
                if to_sleep <= 10:
                    # at least sleep 10 seconds
                    to_sleep = 10
                self.logger.warning("TweepError, sleep {} seconds and try again.".format(to_sleep))
                sleep(to_sleep)
            # retry updating
            self.update_rate_limit_status(err_count + 1)

    def limit_handled(self, cursor, cursor_type):
        """
        limit handled method used to query twitter api in a pagination fashion
        :param cursor: the method cursor, available methods cal be found in Tweepy api
        :param cursor_type: string, indicates the cursor name
        :return: list, results
        """
        # http://docs.tweepy.org/en/v3.8.0/code_snippet.html?highlight=rate%20limits#handling-the-rate-limit-using-cursors
        while True:
            try:
                # next page of results
                yield cursor.next()
            except tweepy.TweepError:
                # Inherits from TweepError, so except TweepError will catch a RateLimitError too.
                if self.rate_limits is None:
                    self.logger.warning("Handle cursor occurs rate limit error, sleep 15 minutes and try again.")
                    sleep(15 * 60)
                else:
                    # Need to update reset time frequently, otherwise this part won't work
                    # Worker updates it.
                    self.update_rate_limit_status()

                    # find out how long to sleep
                    if cursor_type == 'friends':
                        remaining = self.rate_limits['resources']['friends']['/friends/ids']['remaining']
                        to_sleep = self.rate_limits['resources']['friends']['/friends/ids']['reset'] - int(time())
                    elif cursor_type == 'followers':
                        remaining = self.rate_limits['resources']['followers']['/followers/ids']['remaining']
                        to_sleep = self.rate_limits['resources']['followers']['/followers/ids']['reset'] - int(time())
                    # elif cursor_type == 'timeline':
                    else:
                        remaining = self.rate_limits['resources']['statuses']['/statuses/user_timeline']['remaining']
                        to_sleep = self.rate_limits['resources']['statuses']['/statuses/user_timeline']['reset'] - int(
                            time())
                    if remaining:
                        break
                    self.logger.warning(
                        "Handle {} cursor occurs TweepError, sleep {} seconds and try again.".format(cursor_type,
                                                                                                     abs(to_sleep)))
                    sleep(abs(to_sleep))
            except StopIteration:
                # at the last page
                # https://stackoverflow.com/questions/51700960
                break

    def get_followers_ids(self, user_id):
        """
        get the followers ids of a user
        :param user_id: user id
        :return: set, follower ids
        """
        # lock this func in case of occurring rate limit err
        self.lock_followers.acquire()
        # sleep to avoid competition
        self.sleep(int(time()) - self.access_followers, 2)
        # update the accessing time
        self.access_followers = int(time())
        self.lock_followers.release()

        # up to a maximum of 5,000 per distinct request
        # https://developer.twitter.com/en/docs/accounts-and-users/follow-search-get-users/api-reference/get-followers-ids

        return set(self.api.followers_ids(count=5000, user_id=user_id))

    def lookup_statuses(self, **kwargs):
        """
        get set of statuses
        :param kwargs: args
        :return: list, statuses
        """
        try:
            return self.api.statuses_lookup(**kwargs)
        except Exception:
            self.logger.warning(traceback.format_exc())
            return []

    def get_friends_ids(self, user_id):
        """
        get the following user ids of a user
        :param user_id: string or int, user id
        :return: list, friend ids
        """
        # lock this func in case of occurring rate limit err
        self.lock_friends.acquire()
        # sleep to avoid competition
        self.sleep(int(time()) - self.access_friends, 2)
        # update the accessing time
        self.access_friends = int(time())
        self.lock_friends.release()

        # up to a maximum of 5,000 per distinct request
        # https://developer.twitter.com/en/docs/accounts-and-users/follow-search-get-users/api-reference/get-friends-ids

        return set(self.api.friends_ids(count=5000, user_id=user_id))

    def get_user_timeline(self, user_id):
        """
        get a uses's timeline
        :param user_id: string or int, user id
        :return: list, statuses
        """
        # lock this func in case of occurring rate limit err
        self.lock_user_timeline.acquire()
        # sleep to avoid competition
        self.sleep(int(time()) - self.access_user_timeline, 2)
        # update the accessing time
        self.access_user_timeline = int(time())
        self.lock_user_timeline.release()
        # up to a maximum of 200 per distinct request
        # https://developer.twitter.com/en/docs/tweets/timelines/api-reference/get-statuses-user_timeline
        return self.api.user_timeline(count=self.config.user_timeline_max_statues, user_id=user_id,
                                      tweet_mode="extended")

    def lookup_users(self, users_ids):
        """
        get a set of user profiles
        :param users_ids: user ids in list
        :return: list, user ids
        """
        # lock this func in case of occurring rate limit err
        self.lock_lookup_users.acquire()
        # sleep to avoid competition
        self.sleep(int(time()) - self.access_lookup_users, 2)
        # update the accessing time
        self.access_lookup_users = int(time())
        self.lock_lookup_users.release()

        # Note, this method is not in tweepy official doc but in its source file
        # https://developer.twitter.com/en/docs/accounts-and-users/follow-search-get-users/api-reference/get-users-lookup
        # Requests / 15-min window (user auth)	900
        users = []
        for i in range(0, len(users_ids), 100):
            users_res = self.api.lookup_users(user_ids=users_ids[i:i + 100])
            users += users_res
        return users

    def hash(self, source_str):
        """
        generate a hash of a string
        :param source_str: string, source string
        :return: string, the hash of the  source string
        """
        h = hashlib.new(self.config.hash_algorithm)
        h.update(bytes(source_str, 'utf-8'))
        return h.hexdigest()

    @staticmethod
    def sleep(target_time, to_sleep_):
        """
        sleep for at least a minimum time
        :param target_time: target time to sleep
        :param to_sleep_: minimum time
        """
        if target_time <= to_sleep_:
            target_time = to_sleep_
            sleep(target_time)


class StreamListener(tweepy.StreamListener):
    def __init__(self, worker_id, res_queue, log_level, **kw):
        """
        a stream listen which gives the statuses in real time
        :param worker_id: unique worker id
        :param res_queue: results queue used to record stream statuses
        :param log_level: log printing level
        :param kw: params
        """
        self.logger = get_logger('StreamListener', log_level)
        self.id = worker_id
        self.res_queue = res_queue
        self.err_count = 0
        super(StreamListener, self).__init__(**kw)

    def on_status(self, status):
        """
        Called when a new status arrives
        :param status: the new status
        """
        self.res_queue.put(status)
        self.logger.info("[{}] Got stream status: {}".format(self.id, status._json['id_str']))

    def on_error(self, status_code):
        """
        Called when a non-200 status code is returned
        :param status_code: non-200 status code
        """
        self.err_count += 1
        wait_for = self.err_count ** 5
        if wait_for < 120:
            self.logger.warning(
                "[{}] error: {}. Sleep {} seconds".format(self.id, status_code, wait_for))
            sleep(wait_for)
            raise Exception
        else:
            self.logger.warning(
                "[{}] error: {}. {} errs happened, exit.".format(self.id, status_code, self.err_count))
            raise Exception

    def on_connect(self):
        """
        Called once connected to streaming server.

        This will be invoked once a successful response
        is received from the server. Allows the listener
        to perform some work prior to entering the read loop.
        """
        self.logger.debug("[{}] is listening stream.".format(self.id))


if __name__ == '__main__':
    # testing code
    crawler = Crawler(logging.DEBUG)
    for item in crawler.api_keys.items():
        crawler.init(item[0], 0)
        break
    res = crawler.lookup_statuses(id_=['1140944190714630150'], tweet_mode='extended')
    print()
