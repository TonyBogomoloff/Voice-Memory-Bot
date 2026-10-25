"""Publish an append-only Telegram custom-emoji pack owned by the configured user."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--secrets', type=Path, required=True)
    parser.add_argument('--owner', type=int, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if args.owner not in config['allowed_user_ids']:
        raise SystemExit('Pack owner is not an allowed project user')
    secret = json.loads(args.secrets.read_text())
    token = secret['bot_token_your_audio_notes']
    manifest = json.loads((args.assets/'manifest.json').read_text())
    with httpx.Client(timeout=90,trust_env=False) as client:
        def api(method, *, missing=False, files=None, **payload):
            try:
                url='https://api.telegram.org/bot'+token+'/'+method
                result=client.post(url,files=files,data=payload) if files else client.post(url,json=payload)
                body=result.json()
            except (httpx.HTTPError, ValueError):
                raise SystemExit('Telegram transport failed; inspect the set before retrying') from None
            if not body.get('ok'):
                if missing and 'STICKERSET_INVALID' in body.get('description',''):
                    return None
                # Provider messages must not leak a URL or credential.
                print(json.dumps({'method':method,'code':body.get('error_code'),'reason':body.get('description','').replace(token,'[redacted]')},ensure_ascii=False))
                raise SystemExit(1)
            return body['result']
        me=api('getMe')
        if me['username']!='your_audio_notes_bot':
            raise SystemExit('Wrong bot identity')
        name='AudioNotesBlueTape_by_'+me['username']
        old=api('getStickerSet',missing=True,name=name)
        if old:
            if old['sticker_type']!='custom_emoji' or len(old['stickers'])!=len(manifest['icons']):
                raise SystemExit('Existing set differs; refusing to replace/reorder it')
        else:
            stickers=[]
            for item in manifest['icons']:
                path=args.assets/(item['name']+'.png')
                with path.open('rb') as handle:
                    uploaded=api('uploadStickerFile',user_id=str(args.owner),sticker_format='static',
                                 files={'sticker':(path.name,handle,'image/png')})
                stickers.append({'sticker':uploaded['file_id'],'format':'static','emoji_list':[item['emoji']]})
                print('uploaded',item['name'],flush=True)
            api('createNewStickerSet',user_id=args.owner,name=name,title=manifest['title'],
                sticker_type='custom_emoji',needs_repainting=False,stickers=stickers)
            old=api('getStickerSet',name=name)
        if len(old['stickers'])!=len(manifest['icons']):
            raise SystemExit('Published count differs')
        mapping={}
        for item,sticker in zip(manifest['icons'],old['stickers']):
            if sticker['emoji']!=item['emoji'] or sticker['width']!=100 or sticker['height']!=100:
                raise SystemExit('Published asset identity or size differs')
            mapping[item['name']]=sticker['custom_emoji_id']
        ids=list(mapping.values())
        verified=api('getCustomEmojiStickers',custom_emoji_ids=ids)
        if {v['custom_emoji_id'] for v in verified}!=set(ids):
            raise SystemExit('Emoji ID verification failed')
        receipt={'name':name,'title':manifest['title'],'url':'https://t.me/addemoji/'+name,
                 'owner_id':args.owner,'ids':mapping}
        (args.assets/'publication.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'published':receipt['url'],'count':len(mapping),'owner_id':args.owner}))


if __name__=='__main__':
    main()
