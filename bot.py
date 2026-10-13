"""Keep a seven-day TikTok queue in Buffer. Credentials belong in cloud secrets."""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API = 'https://api.buffer.com'
ROOT = Path(__file__).resolve().parent


def api(query, variables):
    token = os.environ.get('BUFFER_API_KEY')
    if not token:
        raise RuntimeError('BUFFER_API_KEY secret is missing; nothing was scheduled.')
    request = Request(API, data=json.dumps({'query': query, 'variables': variables}).encode(),
                      headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
                      method='POST')
    try:
        with urlopen(request, timeout=90) as response:
            result = json.load(response)
    except (HTTPError, URLError, TimeoutError):
        # Never retry a write blindly: the server may have accepted it already.
        raise RuntimeError('Buffer request failed. Re-run to check the remote queue before retrying.') from None
    if result.get('errors'):
        raise RuntimeError('Buffer rejected the GraphQL request: ' +
                           '; '.join(e.get('message', 'unknown error') for e in result['errors']))
    return result['data']


def due(post):
    return datetime.fromisoformat(post['scheduled_at_jakarta']).astimezone(timezone.utc)


def validate(plan):
    posts = plan['posts']
    if len({p['day'] for p in posts}) != len(posts):
        raise RuntimeError('Duplicate day in queue.')
    if len({p['sha256'] for p in posts}) != len(posts):
        raise RuntimeError('Duplicate video in queue.')
    for post in posts:
        when = datetime.fromisoformat(post['scheduled_at_jakarta'])
        if (when.hour, when.minute) != (19, 0) or when.utcoffset() != timedelta(hours=7):
            raise RuntimeError('Queue must use Jakarta 19:00.')
        if not post['caption'].strip() or len(post['caption']) > 2200:
            raise RuntimeError('Missing or oversized caption.')
        if not post['media_url'].startswith('https://raw.githubusercontent.com/acoldmoney-spec/ydacstudio-tiktok-bot/main/'):
            raise RuntimeError('Unexpected media location.')
    ordered = sorted(posts, key=due)
    if any(due(b) - due(a) != timedelta(days=1) for a, b in zip(ordered, ordered[1:])):
        raise RuntimeError('Queue must contain exactly one video per consecutive day.')


def existing_posts(channel):
    query = '''query($input: PostsInput!, $after: String) {
      posts(first: 100, after: $after, input: $input) {
        edges { node { id text dueAt status schedulingType
          metadata { ... on TiktokPostMetadata { isAiGenerated } }
        } }
        pageInfo { hasNextPage endCursor }
      }
    }'''
    after = None
    posts = []
    for _ in range(30):
        result = api(query, {'input': {'organizationId': channel['organizationId'],
                                     'filter': {'channelIds': [channel['id']]}}, 'after': after})['posts']
        posts.extend(edge['node'] for edge in result['edges'])
        if not result['pageInfo']['hasNextPage']:
            return posts
        after = result['pageInfo']['endCursor']
    raise RuntimeError('Too many pages; stopped without scheduling anything.')


def already_present(post, existing):
    matches = [p for p in existing if p['text'].strip() == post['caption'].strip()]
    if len(matches) > 1:
        raise RuntimeError(f'Duplicate remote posts for day {post["day"]}; review before continuing.')
    if not matches:
        return False
    remote = matches[0]
    if remote['status'] not in ('buffer', 'scheduled', 'sent'):
        raise RuntimeError(f'Day {post["day"]} has remote status {remote["status"]}; review required.')
    if remote['schedulingType'] != 'automatic':
        raise RuntimeError(f'Day {post["day"]} requires manual posting; stopped.')
    if not remote.get('metadata', {}).get('isAiGenerated'):
        raise RuntimeError(f'Day {post["day"]} is missing its AI disclosure; stopped.')
    if remote['dueAt'] and datetime.fromisoformat(remote['dueAt'].replace('Z', '+00:00')) != due(post):
        raise RuntimeError(f'Day {post["day"]} has a different scheduled time; stopped.')
    return True


def revision_target(post, existing):
    """Resolve an approved edit to the existing scheduled post, without duplicating it."""
    old_text = post.get('previous_caption')
    if not old_text:
        return None
    matches = [p for p in existing if p['text'].strip() == old_text.strip()]
    if len(matches) > 1:
        raise RuntimeError(f'Duplicate old posts for day {post["day"]}; stopped.')
    if not matches:
        return None
    remote = matches[0]
    if remote['id'] != post['revision_post_id']:
        raise RuntimeError('Approved revision does not match the expected post ID.')
    already_present(dict(post, caption=old_text), [remote])
    if remote['status'] == 'sent':
        raise RuntimeError('Cannot revise an already published video.')
    if any(p['text'].strip() == post['caption'].strip() for p in existing):
        raise RuntimeError('Both original and revised posts exist; review required.')
    return remote


def run(apply=False, now=None):
    plan = json.loads((ROOT / 'queue.json').read_text())
    validate(plan)
    now = now or datetime.now(timezone.utc)
    selected = [p for p in plan['posts'] if now + timedelta(minutes=30) < due(p) <= now + timedelta(days=7)]
    if not apply:
        print(json.dumps({'status': 'DRY_RUN_NOT_SCHEDULED', 'ready_in_window': len(selected),
                          'days': [p['day'] for p in selected], 'total_videos': len(plan['posts'])}))
        return
    channel = api('''query($input: ChannelInput!) {
      channel(input: $input) { id name service organizationId isDisconnected isLocked isQueuePaused }
    }''', {'input': {'id': plan['channel_id']}})['channel']
    if channel['service'].lower() != 'tiktok' or channel['name'].lstrip('@') != 'ydacstudio.id':
        raise RuntimeError('Unexpected channel; nothing was scheduled.')
    if any(channel[k] for k in ('isDisconnected', 'isLocked', 'isQueuePaused')):
        raise RuntimeError('TikTok channel is disconnected, locked or paused.')
    existing = existing_posts(channel)
    if any(p['status'] == 'error' for p in existing):
        raise RuntimeError('A post has failed in Buffer; review it before adding more posts.')
    queued = sum(p['status'] in ('buffer', 'scheduled') for p in existing)
    mutation = '''mutation($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess { post { id text dueAt status schedulingType
          metadata { ... on TiktokPostMetadata { isAiGenerated } }
        } }
        ... on MutationError { message }
      }
    }'''
    edit_mutation = '''mutation($input: EditPostInput!) {
      editPost(input: $input) {
        ... on PostActionSuccess { post { id text dueAt status schedulingType
          metadata { ... on TiktokPostMetadata { isAiGenerated } }
        } }
        ... on MutationError { message }
      }
    }'''
    for post in selected:
        old = revision_target(post, existing)
        if old:
            edit_input = {'id': old['id'], 'text': post['caption'],
                          'assets': [{'video': {'url': post['media_url']}}],
                          'schedulingType': 'automatic', 'aiAssisted': True,
                          'metadata': {'tiktok': {'isAiGenerated': True}}}
            result = api(edit_mutation, {'input': edit_input})['editPost']
            if not result.get('post'):
                raise RuntimeError('Buffer rejected the revision: ' + result.get('message', 'unknown error'))
            if result['post']['id'] != old['id']:
                raise RuntimeError('Revision returned an unexpected post ID.')
            already_present(post, [result['post']])
            existing[existing.index(old)] = result['post']
            print(f'Day {post["day"]}: revised existing post {old["id"]}; AI disclosure confirmed.')
            continue
        if already_present(post, existing):
            print(f'Day {post["day"]}: already scheduled.')
            continue
        if queued >= 9:
            raise RuntimeError('Buffer queue is nearly full; stopped below the Free plan limit.')
        request_input = {'channelId': channel['id'], 'text': post['caption'],
                         'assets': [{'video': {'url': post['media_url']}}],
                         'schedulingType': 'automatic', 'mode': 'customScheduled',
                         'dueAt': due(post).isoformat().replace('+00:00', 'Z'),
                         'needsApproval': False, 'aiAssisted': True,
                         'metadata': {'tiktok': {'isAiGenerated': True}}}
        result = api(mutation, {'input': request_input})['createPost']
        if result.get('message') or not result.get('post'):
            raise RuntimeError('Buffer rejected a post: ' + result.get('message', 'unknown error'))
        remote = result['post']
        if not already_present(post, [remote]):
            raise RuntimeError('Buffer did not confirm the scheduled post.')
        existing.append(remote)
        queued += 1
        print(f'Day {post["day"]}: confirmed {remote["id"]} at {remote["dueAt"]}.')
    if now > max(due(p) for p in plan['posts']):
        print('All 60 planned days have passed. Add new content; videos are not repeated automatically.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        run(apply=args.apply)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
