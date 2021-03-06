#!/usr/bin/env python2.7
# -*- coding: utf-8


import time, sys, os
from functools import partial
from logging import getLogger
from multiprocessing import Pool
import requests, json

import vk
from settings import settings
from sys_utilst import load, save

from grammar_utils import normalize_sentence
from w2v_model import in_vocab
from pictures.generator import generate_image
from style import RHYME_SYSTEM
from poet import build


logger = getLogger(__name__)


MAX_USERS_PER_REQUEST = 1000
MAX_MESSAGES_PER_REQUEST = 200

NOT_FOLLOW_MSG = u'Сначала вступи в паблик.'
BAD_WORDS_MSG = u'Я таких слов не знаю.'
NO_BLOCKS_MSG = u'Об этом мало пишут.'


def get_all_items(max_count_per_request, getter):
    total_count = max_count_per_request
    offset = 0
    items = []
    while offset < total_count:
        response = getter(offset=offset, count=max_count_per_request)
        items.extend(response['items'])
        total_count = response['count']
        offset += max_count_per_request
        time.sleep(settings['sleep'])

    return items


def send_image(api, user_id, sign, post, title):
    img = generate_image(post, sign=sign, title=title)
    img = {'photo': ('img.png', img)}
    upload_url = api.photos.getMessagesUploadServer()['upload_url']
    response = json.loads(requests.post(upload_url, files=img).text)

    photo_data = api.photos.saveMessagesPhoto(photo=response['photo'],
                                              hash=response['hash'],
                                              server=response['server'])

    attach = 'photo%s_%s' % (photo_data[0]['owner_id'], photo_data[0]['id'])
    api.messages.send(user_id=user_id, attachment=[attach])
    time.sleep(settings['sleep'])


def build_blocks(source_file_name, followers, used_cache, message):
    data = load(source_file_name)
    mask_to_sentences = data['mask_to_sentences']
    matched_masks = data['matched_masks']

    rs_name = os.environ.get('style', settings.get('style', 'random')).upper()
    system = getattr(RHYME_SYSTEM, rs_name, None)
    if system is None:
        from random import choice
        rs_name, system = choice([(k, v) for k, v in RHYME_SYSTEM.__dict__.iteritems() if '_' not in k])

    logger.info('%s: %s' % (rs_name, system))
    user_id = message['user_id']
    init_sign = followers.get(user_id)
    if not init_sign:
        return False, (user_id, NOT_FOLLOW_MSG)

    words = normalize_sentence(message['body'])
    logger.info('%s words %s' % (rs_name, ' '.join(words)))
    good_words = set([w for w in words if in_vocab(w)])
    if not (0 < len(good_words) < 5):
        return False, (user_id, BAD_WORDS_MSG)

    title = ' '.join(words)
    logger.info('good words %s' % title)
    _, user_cache = used_cache.get(user_id, (None, set()))
    blocks = build(mask_to_sentences, matched_masks, system, user_cache, good_words)
    if not blocks:
        return False, (user_id, NO_BLOCKS_MSG)

    return True, (user_id, blocks, title)


def get_best_block(used_cache, followers, user_id, blocks):
    curr_time = int(time.time())
    _, curr_user_cache = used_cache.setdefault(user_id, (curr_time, set()))
    for block in blocks:
        post = [sentence['text'] for sentence in block]

        s_post = set(post)
        if curr_user_cache & s_post:
            continue

        authors = {user_id} | set(settings['tester_ids'])
        if settings['spam_mode']:
            for sentence in block:
                author_id = sentence['user_id']
                if author_id in followers:
                    last_time, user_cache = used_cache.setdefault(author_id, (curr_time, set()))
                    reply_delay = curr_time - last_time
                    if not (user_cache & s_post) and reply_delay > settings['auto_reply_delay']:
                        authors.add(author_id)

        authors |= set(settings['tester_ids'])
        return True, (authors, post)

    return False, NO_BLOCKS_MSG


def loop(source_file_name):
    session = vk.Session(settings['group_token'])
    api = vk.API(session, lang='ru', v='5.64')

    followers = get_all_items(
        MAX_USERS_PER_REQUEST,
        lambda **kwargs : api.groups.getMembers(group_id=settings['group_id'], fields='domain', **kwargs)
    )
    followers = {u['id']: '%s %s' % (u['first_name'], u['last_name']) for u in followers}

    all_messages = get_all_items(
        MAX_MESSAGES_PER_REQUEST,
        lambda **kwargs : api.messages.getDialogs(unanswered=1, preview_length=20, **kwargs)
    )

    if not all_messages:
        return 0

    try:
        used_cache = load('used_cache.bin')
    except IOError:
        used_cache = {}

    curr_time = int(time.time())
    messages = []
    cache_was_modified = False
    for m in all_messages:
        msg = m['message']
        u_id = msg['user_id']
        if u_id not in settings['tester_ids'] or 'clear' not in msg['body']:
            messages.append(msg)
            continue

        used_cache[u_id] = (curr_time, set())
        cache_was_modified = True
        try:
            api.messages.send(user_id=u_id, message='done')
        except vk.exceptions.VkAPIError as error:
            logger.error('send to %s %s' % (followers[u_id], error.message))
            continue

    if (cache_was_modified):
        save(used_cache, 'used_cache.bin')

    build = partial(build_blocks, source_file_name, followers, used_cache)
    processes = settings['processes']
    if processes > 1:
        pool = Pool(processes=processes)
        results = pool.map(build, messages)
    else:
        results = [build(msg) for msg in messages]

    for is_ok, result in results:
        if not is_ok:
            user_id, message = result
            api.messages.send(user_id=user_id, message=message)
            time.sleep(settings['sleep'])
            continue

        user_id, blocks, title = result
        is_ok, result = get_best_block(used_cache, followers, user_id, blocks)
        if not is_ok:
            api.messages.send(user_id=user_id, message=result)
            time.sleep(settings['sleep'])
            continue

        user_ids, post = result
        s_post = set(post)
        for u_id in user_ids:
            is_tester = u_id in settings['tester_ids']
            sign = followers[user_id if is_tester else u_id]
            if is_tester and user_id == u_id:
                sign = None
            try:
                _, user_cache = used_cache.setdefault(u_id, (curr_time, set()))
                send_image(api, u_id, sign, post, title)
                used_cache[u_id] = (curr_time, user_cache | s_post)
                logger.info('send to %s' % followers[u_id])
            except vk.exceptions.VkAPIError as error:
                logger.error('send to %s %s' % (followers[u_id], error.message))
                continue
        save(used_cache, 'used_cache.bin')
    return 0


if __name__ == '__main__':
    sys.exit(loop(sys.argv[1]))
