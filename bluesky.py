import os
from atproto import Client
import json
from datetime import datetime
import requests

class bluesky():

    def __init__(self, logger, botname, username, password, PROCESSED_NOTIFICATIONS_FILE):
        # Initialize the bluesky class with the given parameters
        self.client = Client()
        self.botname = botname
        self.client.login(username, password)
        self.PROCESSED_NOTIFICATIONS_FILE = PROCESSED_NOTIFICATIONS_FILE
        self.processed_notifications = self.load_processed_notifications()
        self.logger = logger

    def load_processed_notifications(self):
        if os.path.exists(self.PROCESSED_NOTIFICATIONS_FILE):
            with open(self.PROCESSED_NOTIFICATIONS_FILE, 'r') as f:
                return set(json.load(f))
        return set()

    def save_processed_notifications(self):
        with open(self.PROCESSED_NOTIFICATIONS_FILE, 'w') as f:
            json.dump(list(self.processed_notifications), f)

    def upload_and_create_image_blob(self, image_path):
        with open(image_path, 'rb') as f:
            image_data = f.read()
        image_blob = self.client.upload_blob(image_data)
        self.logger.info("Uploaded image blob: %s", image_blob)

        return {
            '$type': 'blob',
            'ref': {'$link': image_blob.blob.ref.link},
            'mimeType': image_blob.blob.mime_type,
            'size': image_blob.blob.size
        }

    def download_image(self, author_did, cid, alt_link, save_path='results/downloaded_image.jpg'):
        try:
            image_url = f"https://cdn.bsky.app/img/feed_fullsize/plain/{author_did}/{cid}"
            headers = {'User-Agent': 'YourBotName/1.0'}
            response = requests.get(image_url, headers=headers)
            if response.status_code != 200 and alt_link:
                response = requests.get(alt_link, headers=headers)
            if response.status_code == 200:
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with open(save_path, 'wb') as file:
                    file.write(response.content)
                self.logger.info(f"Image downloaded from {image_url}: {save_path}")
                return save_path
            else:
                self.logger.error(f"Failed to download image. Status code: {response.status_code} URL: {image_url}")
                return None
        except Exception as e:
            self.logger.error(f"Error downloading image: {e}")
            return None

    def Check_valid_notifications(self):
        notifications = self.client.app.bsky.notification.list_notifications()['notifications']
        for notification in notifications:
            if notification['uri'] in self.processed_notifications:
                continue
            self.processed_notifications.add(notification['uri'])
            self.save_processed_notifications()

            if notification['reason'] == 'mention':
                thread = self.client.app.bsky.feed.get_post_thread({'uri': notification['uri']})
                post = thread['thread']['post']
                record = post['record']
                text = record.text.lower()

                root_uri = post['uri']
                root_cid = post['cid']
                parent_uri = post['uri']
                parent_cid = post['cid']
                post_id = { 'root_uri': root_uri, 'root_cid': root_cid, 'parent_uri': parent_uri, 'parent_cid': parent_cid }

                if self.botname in text:
                    self.logger.info(f"Bot was tagged in a post: {text}")

                    # If there's no embed on this post, try treating it as a comment
                    if not getattr(record, 'embed', None):
                        try:
                            if thread['thread'].get('parent') is None:
                                return post_id, None
                            if post['author']['handle'] == thread['thread']['parent']['post']['author']['handle']:
                                post = thread['thread']['parent']['post']
                                record = post['record']
                            else:
                                return post_id, None
                        except Exception as e:
                            self.logger.error("Error finding parent post: %s", e)
                            return post_id, None

                    embed = record.embed

                    # === BEGIN BUG FIX ===
                    try:
                        # Detect if embed is a quoted Bluesky post (has .record) or an external link
                        if hasattr(embed, 'record') and embed.record is not None:
                            # .record exists only on Bluesky embed, so it's safe to use
                            quoted_uri = embed.record.uri
                        elif hasattr(embed, 'external') and embed.external is not None:
                            # external is a link preview with no .record => skip
                            self.logger.info("Embed is external link (%s), skipping.", embed.external.uri)
                            return post_id, None
                        else:
                            # Neither record nor external present => unexpected embed type
                            self.logger.error("Embed has no .record or .external: %r", embed)
                            return post_id, None

                        # Fetch the quoted post which should have images
                        quoted_thread = self.client.app.bsky.feed.get_post_thread({'uri': quoted_uri})
                        post2 = quoted_thread['thread']['post']
                        embed2 = post2['record'].embed

                        # Pull images from the quoted embed
                        if hasattr(embed2, 'images') and embed2.images:
                            images = embed2.images
                            alt_link = images[0].fullsize
                        elif hasattr(embed2, 'media') and getattr(embed2.media, 'images', None):
                            images = embed2.media.images
                            alt_link = None
                        else:
                            # No images found => bail
                            self.logger.error("Quoted post has no images: %r", embed2)
                            return post_id, None

                    except Exception as e:
                        # Log any errors in attempting to extract quoted images
                        self.logger.error("Error finding image in quoted post: %s", e)
                        return post_id, None
                    # === END BUG FIX ===

                    # Download the first image
                    image_cid = images[0].image.ref.link if hasattr(images[0], 'image') else ''
                    author_did = post2['author']['did']
                    downloaded_image_path = self.download_image(author_did, image_cid, alt_link)

                    # Adjust roots if reply
                    if getattr(post['record'], 'reply', None):
                        root_ref = post['record'].reply.root
                        post_id['root_uri'] = root_ref.uri
                        post_id['root_cid'] = root_ref.cid

                    return post_id, downloaded_image_path

        return None

    def post_reply(self, images_list, post_text, post_id):
        image_embeds = []
        for image_path, alt_text in images_list:
            if image_path and os.path.exists(image_path):
                blob_ref = self.upload_and_create_image_blob(image_path)
                image_embeds.append({ 'image': blob_ref, 'alt': alt_text })

        record = {
            '$type': 'app.bsky.feed.post',
            'text': post_text,
            'createdAt': datetime.utcnow().isoformat() + 'Z',
            'reply': {
                'root': { 'uri': post_id['root_uri'], 'cid': post_id['root_cid'] },
                'parent': { 'uri': post_id['parent_uri'], 'cid': post_id['parent_cid'] }
            }
        }

        if image_embeds:
            record['embed'] = { '$type': 'app.bsky.embed.images', 'images': image_embeds }

        facets = self.add_mention_facets(post_text)
        if facets:
            record['facets'] = facets

        try:
            self.client.com.atproto.repo.create_record({
                'repo': self.client.me.did,
                'collection': 'app.bsky.feed.post',
                'record': record
            })
            self.logger.info("Replied to the post with images.")
        except Exception as e:
            self.logger.error("Error creating reply: %s", e)
            self.logger.error("Record: %s", record)

    def add_mention_facets(self, post_text, mention_str="@quantumkat.bsky.social", mention_did="did:plc:bqvcty4gfx5s2b4gvlff6ikp"):
        start = post_text.find(mention_str)
        if start == -1:
            return None
        end = start + len(mention_str)
        return [{
            'index': {'byteStart': start, 'byteEnd': end},
            'features': [{ '$type': 'app.bsky.richtext.facet#mention', 'did': mention_did }]
        }]

    def repost_original_post(self, uri, cid):
        repost_record = {
            '$type': 'app.bsky.feed.repost',
            'subject': { 'uri': uri, 'cid': cid },
            'createdAt': datetime.utcnow().isoformat() + 'Z'
        }
        try:
            self.client.com.atproto.repo.create_record({
                'repo': self.client.me.did,
                'collection': 'app.bsky.feed.repost',
                'record': repost_record
            })
            self.logger.info(f"Reposted the post with URI: {uri}")
        except Exception as e:
            self.logger.error("Error creating repost: %s", e)
            self.logger.error("Record: %s", repost_record)
