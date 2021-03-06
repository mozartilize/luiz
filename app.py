import os
import logging
import asyncio
import io  # noqa
import signal
import ssl as ssl_lib
from functools import namedtuple

import certifi
import slack
import httpx
from sqlalchemy import create_engine
from sqlalchemy.sql import table, select, literal_column, bindparam

try:
    from dotenv import load_dotenv
except ImportError:
    pass
else:
    load_dotenv()

# TODO: use redis or setup crontask to clean these memory storages
attachment_message = {}

users_info = {}


FileInfo = namedtuple('FileInfo',
                      'type id name permalink url_private size content')

db = create_engine(os.environ["DATABASE_URL"], pool_recycle=600)


def get_access_token(team_id):
    sql = select([literal_column("access_token")])\
        .select_from(table("tokens"))\
        .where(literal_column("team_id") == bindparam("team_id"))\
        .order_by(literal_column("timestamp").desc()).limit(1)
    with db.engine.connect() as conn:
        row = conn.execute(sql, team_id=team_id).fetchone()
    return row.access_token


async def get_userinfo(web_client, user_id):
    try:
        return users_info[user_id]
    except KeyError:
        userinfo_resp = await web_client.users_info(user=user_id)
        users_info[user_id] = userinfo_resp.data['user']
        return users_info[user_id]


def is_attachment_message(data):
    for blk in data.get('blocks', []):
        for el in blk.get('elements', []):
            for subel in el.get('elements', []):
                if subel.get('type') == 'link':
                    yield subel['url']


def is_file_message(data):
    for filedata in data.get('files', []):
        info = (
            filedata['id'],
            filedata['name'],
            filedata['permalink'],
            filedata['url_private'],
            filedata['size'],
            None,  # content
        )
        if 'image/' in filedata['mimetype']:
            yield FileInfo('image', *info)
        elif 'video/' in filedata['mimetype']:
            yield FileInfo('video', *info)
    yield from ()  # return empty generator if not match any


async def delete_nsfw_and_clone_it_to_thread(web_client, info, payload):
    user = await get_userinfo(web_client, info["user_id"])
    async with httpx.Client(headers={
        'Authorization': 'Bearer %s' % (info["access_token"])
    }) as requests:
        delete_resp = await requests.post(  # noqa
            'https://slack.com/api/chat.delete',
            params=dict(
                channel=info["channel"],
                ts=payload["ts"],
            )
        )
        msg = await web_client.chat_postMessage(
            channel=info["channel"],
            text='This message has been hidden')
        files = payload.get("files")
        text = payload["text"]
        text2 = ''
        if files:
            if any(finfo.size > 15 * 1024 * 1024 for finfo in files.values()):
                for finfo in files.values():
                    if text != '':
                        text += '\n'
                    text += '<%s|%s>' % (finfo.permalink, finfo.name)
                if text != '':
                    text += '\n'
                text += '\n<@%s> Please copy and paste the link(s) above to ' \
                    'this thread if you want to reshare. ' \
                    'Sorry for inconvenience.' % (info["user_id"])
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

                async def do_upload(fileinfo):
                    resp = await web_client.files_upload(
                        filename=fileinfo.name,
                        title=fileinfo.name,
                        file=fileinfo.content,
                    )
                    return resp
                for fileid, fileinfo in files.items():
                    # delete old files, maintain storage
                    await requests.post('https://slack.com/api/files.delete',
                                        data={'file': fileid})
                    task = asyncio.create_task(do_upload(fileinfo))
                    task.add_done_callback(done_callback)
                    await task
        await web_client.chat_postMessage(  # noqa
            **{
                'channel': info["channel"],
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
                    'channel': info["channel"],
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

    # do nothing on thread
    if data.get('thread_ts'):
        return

    if subtype == 'message_changed':
        ts = data['previous_message']['ts']
        try:
            attachment_message[data['message']['team']][channel_id][ts]
        except KeyError:
            return
        attachments = data['message'].get('attachments', [])
        is_nsfw = False
        async with httpx.Client(headers={
            'apikey': os.getenv("VISION_APIKEY")
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
            access_token = get_access_token(data['message']["team"])
            info = {
                "channel": channel_id,
                "access_token": access_token,
                "user_id": data['message']['user']
            }
            payload_ = {
                "ts": ts,
                "text": data['previous_message']['text'],
            }
            await delete_nsfw_and_clone_it_to_thread(
                web_client, info, payload_)
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

        access_token = get_access_token(data["team"])
        is_nsfw = False
        files = {}
        async with httpx.Client(headers={
            'Authorization': 'Bearer %s' % (access_token)
        }) as requests, httpx.Client(headers={
            'apikey': os.getenv("VISION_APIKEY")
        }) as requests2:
            for fileinfo in is_file_message(data):
                resp = await requests.get(fileinfo.url_private)
                check_resp = await requests2.post(
                    'https://api.uploadfilter.io/v1/nudity',
                    files={
                        'file': io.BytesIO(resp.content)
                    }
                )
                files[fileinfo.id] = FileInfo(
                    *tuple(fileinfo)[:-1], resp.content)
                if check_resp.status_code == 200:
                    result = check_resp.json()['result']
                    if result['value'] >= 0.35:
                        is_nsfw = True
        if is_nsfw:
            web_client = payload["web_client"]
            info = {
                "channel": channel_id,
                "access_token": access_token,
                "user_id": data['user']
            }
            payload_ = {
                "ts": data['ts'],
                "text": data['text'],
                "files": files
            }
            await delete_nsfw_and_clone_it_to_thread(
                web_client, info, payload_)


async def inf_loop():
    logger = logging.getLogger()
    while 1:
        try:
            logger.info("Ping Pong! I'm alive")
            await asyncio.sleep(900)
        except asyncio.CancelledError:
            break

if __name__ == "__main__":
    import uvloop
    uvloop.install()
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    logger.addHandler(logging.StreamHandler())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    slack_token = os.environ["SLACK_BOT_TOKEN"]
    ssl_context = ssl_lib.create_default_context(cafile=certifi.where())
    rtm_client = slack.RTMClient(
        token=slack_token, ssl=ssl_context, run_async=True, loop=loop
    )

    tasks = asyncio.gather(rtm_client.start(), inf_loop())

    def callback(signum, frame):
        tasks.cancel()
        logger.warning("Cancelling tasks...")

    signal.signal(signal.SIGINT, callback)
    signal.signal(signal.SIGTERM, callback)
    try:
        loop.run_until_complete(tasks)
    except asyncio.CancelledError as e:
        logger.error(e)
    finally:
        logger.info("Quitting... Bye!")
        loop.close()
