import os
import logging
import asyncio
import io  # noqa
import ssl as ssl_lib

import certifi
import slack
import httpx

attachment_message = {}

users_info = {}

access_token = 'xoxp-624137142464-612712054882-832198144609-' \
               '67532ce4e3fe5d99c570cc4eeb70811d'


async def get_userinfo(web_client, user_id):
    try:
        return users_info[user_id]
    except KeyError:
        userinfo_resp = await web_client.users_info(user=user_id)
        users_info[user_id] = userinfo_resp.data['user']
        return users_info[user_id]


def is_attachment_message(data):
    for blk in data['blocks']:
        for el in blk['elements']:
            for subel in el['elements']:
                if subel['type'] == 'link':
                    yield subel['url']


def is_file_message(data):
    for filedata in data.get('files', []):
        info = (
            filedata['permalink'],
            filedata['url_private'],
            filedata['size'],
            filedata['name'],
            filedata['id']
        )
        if 'image/' in filedata['mimetype']:
            yield ('image', *info)
        elif 'video/' in filedata['mimetype']:
            yield ('video', *info)


async def delete_nsfw_and_clone_it_to_thread(
    web_client, channel, user_id, text, ts, files=None
):
    user = await get_userinfo(web_client, user_id)
    async with httpx.AsyncClient(headers={
        'Authorization': 'Bearer %s' % (access_token)
    }) as requests:
        delete_resp = await requests.post(  # noqa
            'https://slack.com/api/chat.delete',
            params=dict(
                channel=channel,
                ts=ts,
            )
        )
        msg = await web_client.chat_postMessage(
            channel=channel,
            text='This message has been hidden')
        text2 = ''
        if files:
            if any(finfo[3] > 15 * 1024 * 1024 for finfo in files.values()):
                for finfo in files.values():
                    if text != '':
                        text += '\n'
                    text += '<%s|%s>' % (
                        finfo[1],
                        finfo[-3]
                    )
                if text != '':
                    text += '\n'
                text += '\n<@%s> Please copy and paste the links above to ' \
                    'this thread if you want to reshare. ' \
                    'Sorry for inconvenience.' % (user_id)
            else:
                def done_callback(fut):
                    nonlocal text2
                    result = fut.result()
                    if text2 != '':
                        text2 += '\n'
                    text2 += '<%s|%s>' % (
                        result['file']['permalink'],
                        result['file']['name']
                    )
                for fileid, fileinfo in files.items():
                    await requests.post('https://slack.com/api/files.delete',
                                        data=dict(file=fileid))

                    async def do_upload(fileinfo):
                        resp = await web_client.files_upload(
                            filename=fileinfo[-3],
                            title=fileinfo[-3],
                            file=fileinfo[-1].read(),
                        )
                        return resp
                    task = asyncio.create_task(do_upload(fileinfo))
                    task.add_done_callback(done_callback)
                    await task
        await web_client.chat_postMessage(  # noqa
            **{
                'channel': channel,
                'thread_ts': msg.data['ts'],
                'text': text if text else 'shares file',
                'username': user['profile']['display_name'],
                'icon_url': user['profile']['image_192'],
                'unfurl_links': True,
                'unfurl_media': True,
            }
        )
        if files and text2:
            await web_client.chat_postMessage(  # noqa
                **{
                    'channel': channel,
                    'thread_ts': msg.data['ts'],
                    'text': text2,
                    'as_user': True,
                }
            )


@slack.RTMClient.run_on(event="message")
async def message(**payload):
    """Display the onboarding welcome message after receiving a message
    that contains "start".
    """
    data = payload["data"]
    channel_id = data['channel']
    subtype = data.get('subtype')

    if data.get('thread_ts'):
        return

    if subtype == 'message_changed':
        ts = data['previous_message']['ts']
        try:
            attachment_message[data['message']['team']][channel_id][ts]
        except KeyError:
            return
        attachments = data['message']['attachments']
        is_nsfw = False
        async with httpx.AsyncClient(headers={
            'apikey': '55ac3e80-110a-11ea-a7d5-6d78e49dec78'
        }) as requests:
            for att in attachments:
                url = att.get('thumb_url') or att.get('image_url')
                if not url:
                    continue
                check_resp = await requests.get(
                    'https://api.uploadfilter.io/v1/nudity',
                    params={'url': url}
                )
                if check_resp.status_code == 200:
                    result = check_resp.json()['result']
                    if result['value'] >= 0.35:
                        is_nsfw = True
                        break
        if is_nsfw:
            web_client = payload["web_client"]
            user_id = data['message']['user']
            text = data['previous_message']['text']
            await delete_nsfw_and_clone_it_to_thread(
                web_client, channel_id, user_id, text, ts)
    elif subtype == 'bot_message':
        pass
    elif subtype is None:  # not bot
        if is_attachment_message(data) is not None:
            teamdata = attachment_message.get(data['team'])
            if not teamdata:
                attachment_message[data['team']] = teamdata = {}
                teamdata[data.get("channel")] = channeldata = {}
            else:
                channeldata = teamdata.get(data.get("channel"))
                if not channeldata:
                    teamdata[data.get("channel")] = channeldata = {}
            channeldata[data['ts']] = 'foo'

        is_nsfw = False
        files = {}
        async with httpx.AsyncClient(headers={
            'Authorization': 'Bearer %s' % (access_token)
        }) as requests, httpx.AsyncClient(headers={
            'apikey': '55ac3e80-110a-11ea-a7d5-6d78e49dec78'
        }) as requests2:
            for fileinfo in is_file_message(data):
                url = fileinfo[2]
                resp = await requests.get(url)
                filebytes = io.BytesIO(resp.content)
                check_resp = await requests2.post(
                    'https://api.uploadfilter.io/v1/nudity',
                    files={
                        'file': filebytes
                    }
                )
                # print(check_resp.json())
                files[fileinfo[-1]] = (*fileinfo, filebytes)
                is_nsfw = True
                if check_resp.status_code == 200:
                    result = check_resp.json()['result']
                    if result['value'] >= 0.35:
                        is_nsfw = True
        if is_nsfw:
            web_client = payload["web_client"]
            user_id = data['user']
            ts = data['ts']
            text = data['text']
            await delete_nsfw_and_clone_it_to_thread(
                web_client,
                channel_id,
                user_id,
                text,
                ts,
                files=files
            )


if __name__ == "__main__":
    import uvloop
    uvloop.install()
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    logger.addHandler(logging.StreamHandler())
    ssl_context = ssl_lib.create_default_context(cafile=certifi.where())
    slack_token = os.environ["SLACK_BOT_TOKEN"]
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    rtm_client = slack.RTMClient(
        token=slack_token, ssl=ssl_context, run_async=True, loop=loop
    )
    loop.run_until_complete(rtm_client.start())
