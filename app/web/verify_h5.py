"""Public, capability-protected ownership page; independent of admin sessions."""
from __future__ import annotations

import json
import base64
import io
import secrets
import time
from collections import OrderedDict

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>B站账号归属验证</title><style nonce="__NONCE__">body{font:16px system-ui;background:#faf8ef;color:#183c31;margin:0;padding:32px 16px}main{max-width:600px;margin:auto;background:white;border:1px solid #ddd9ca;border-radius:18px;padding:28px}h1{font-size:26px}code{display:block;overflow-wrap:anywhere;background:#eff5ef;padding:16px;border-radius:8px}button{background:#215c49;color:white;border:0;border-radius:8px;padding:12px 20px;margin:12px 8px 0 0;cursor:pointer}#status{white-space:pre-wrap;line-height:1.7}a{color:#235f49}</style>
<main><h1>B站账号归属验证</h1><p>仅使用机器人提供的专属链接。验证不会替代本群的其他审核条件。</p><p id="identity"></p><p id="instruction"></p><code id="code" hidden></code><img id="qr" width="240" height="240" alt="使用B站客户端扫描" hidden><a id="target" rel="noreferrer noopener" target="_blank" hidden>打开验证位置</a><div><button id="copy" hidden>复制验证码</button><button id="check" hidden>我已发送，检查结果</button><button id="start-qr" hidden>生成扫码二维码</button></div><p id="status" role="status"></p><small>入群答案只填写B站昵称或UID，请不要添加说明或验证码。扫码凭证仅用于本次身份验证，不会写入机器人管理员账号。</small></main>
<script nonce="__NONCE__">'use strict';
const cap=new URLSearchParams(location.hash.slice(1));let id=cap.get('id'),secret=cap.get('secret');
if(id&&secret){sessionStorage.setItem('gk-verify',JSON.stringify({id,secret}));history.replaceState(null,'',location.pathname)}else{try{({id,secret}=JSON.parse(sessionStorage.getItem('gk-verify')||'{}'))}catch{}}
const status=document.querySelector('#status');
const apiBase=location.pathname.endsWith('/')?location.pathname.slice(0,-1):location.pathname;
async function api(path,data={}){const response=await fetch(apiBase+'/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({challenge_id:id,secret,...data})});const result=await response.json();if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'暂时无法检查验证结果');return result}
async function load(check=false){if(!id||!secret){status.textContent='请重新提交入群申请，并打开机器人反馈的完整验证链接。';return}
const button=document.querySelector('#check');button.disabled=true;
try{const data=await api(check?'check':'state');
status.textContent=data.detail||'';if(['expired','verified'].includes(data.state)){if(data.state==='verified'){document.querySelector('#code').hidden=true;button.hidden=true;document.querySelector('#copy').hidden=true;status.textContent='验证成功。请在有效期内用同一QQ与UID重新提交入群申请；答案只填写昵称或UID。'}return}
document.querySelector('#identity').textContent=`群号 ${data.group_id} · QQ ${data.qq} · B站UID ${data.uid}`;
if(data.mode==='qr'){document.querySelector('#instruction').textContent='请使用上述UID对应的B站账号扫码并确认登录。本次凭证只在内存中验证身份后丢弃；验证通过后重新检查本群规则，原申请状态明确时自动处理，否则提示重新申请。';document.querySelector('#start-qr').hidden=false;return}
document.querySelector('#instruction').textContent=data.mode==='dm'?'请使用上述B站账号，向收信UID '+data.receiver_uid+' 发送以下验证码，单独发送整行。':'请使用上述B站账号，在指定动态的评论区单独发送以下验证码。';
const code=document.querySelector('#code');code.textContent=data.code;code.hidden=false;document.querySelector('#copy').hidden=false;button.hidden=false;
const target=document.querySelector('#target');const url=data.mode==='dm'?'https://message.bilibili.com/#/whisper/mid'+encodeURIComponent(data.receiver_uid):data.dynamic_url;if(typeof url==='string'&&url.startsWith('https://')){target.href=url;target.hidden=false}
}catch(error){status.textContent=error.message}finally{button.disabled=false}}
document.querySelector('#copy').onclick=async()=>{try{await navigator.clipboard.writeText(document.querySelector('#code').textContent);status.textContent='已复制验证码'}catch{status.textContent='请手动复制上方验证码'}};
document.querySelector('#check').onclick=()=>load(true);
let qrTimer=null;document.querySelector('#start-qr').onclick=async()=>{if(qrTimer)clearTimeout(qrTimer);const button=document.querySelector('#start-qr');button.disabled=true;try{const data=await api('qr/start');const image=document.querySelector('#qr');image.src=data.image;image.hidden=false;status.textContent='请使用B站客户端扫码，并确认本次身份验证登录。';
async function poll(){try{const result=await api('qr/poll',{qr_session_id:data.session_id});if(['waiting','confirming'].includes(result.state)){status.textContent=result.state==='confirming'?'请在B站客户端确认登录':'等待扫码';qrTimer=setTimeout(poll,3000)}else{status.textContent=result.detail||({expired:'二维码已过期，请重新生成',verified:'验证成功，请查看QQ入群结果',unavailable:'B站接口暂时不可用，请稍后重试'}[result.state]||'请重新扫码');image.hidden=true;button.disabled=false}}catch(error){status.textContent=error.message;button.disabled=false}}
qrTimer=setTimeout(poll,3000)}catch(error){status.textContent=error.message;button.disabled=false}};load();</script></html>'''


def install_verify_routes(app, runtime):
    attempts = OrderedDict()

    async def payload(request):
        peer = request.client.host if request.client else 'unknown'
        now = time.monotonic()
        while attempts and next(iter(attempts.values()))['at'] < now - 60:
            attempts.popitem(last=False)
        record = attempts.pop(peer, {'at': now, 'count': 0})
        record['count'] += 1
        attempts[peer] = record
        if record['count'] > 180 or len(attempts) > 4096:
            raise HTTPException(429, '请求过于频繁，请稍后重试')
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 2048:
                raise HTTPException(413, '请求过大')
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HTTPException(400, '无效的请求内容') from None
        if not isinstance(data, dict) or set(data) - {'challenge_id', 'secret', 'qr_session_id'}:
            raise HTTPException(400, '无效的验证请求')
        return data

    def headers(nonce=''):
        return {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff',
                'Content-Security-Policy': "default-src 'none'; script-src 'nonce-" + nonce
                + "'; style-src 'nonce-" + nonce + "'; img-src data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"}

    @app.get('/verify', response_class=HTMLResponse)
    async def page():
        nonce = secrets.token_urlsafe(18)
        return HTMLResponse(PAGE.replace('__NONCE__', nonce), headers=headers(nonce))

    async def challenge(request, check):
        data = await payload(request)
        if runtime.ownership is None:
            raise HTTPException(503, '验证服务尚未就绪')
        try:
            result = await runtime.ownership.challenge(data.get('challenge_id'), data.get('secret'), check=check)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return JSONResponse(result, headers=headers())

    @app.post('/verify/state')
    async def state(request: Request):
        return await challenge(request, False)

    @app.post('/verify/check')
    async def check(request: Request):
        return await challenge(request, True)

    @app.post('/verify/qr/start')
    async def qr_start(request: Request):
        data = await payload(request)
        if runtime.ownership is None:
            raise HTTPException(503, '验证服务尚未就绪')
        try:
            result = await runtime.ownership.start_qr(data.get('challenge_id'), data.get('secret'))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        import qrcode
        from qrcode.image.svg import SvgPathImage
        output = io.BytesIO()
        qrcode.make(result['url'], image_factory=SvgPathImage).save(output)
        return JSONResponse({**result, 'image': 'data:image/svg+xml;base64,' + base64.b64encode(output.getvalue()).decode()}, headers=headers())

    @app.post('/verify/qr/poll')
    async def qr_poll(request: Request):
        data = await payload(request)
        if runtime.ownership is None:
            raise HTTPException(503, '验证服务尚未就绪')
        try:
            result = await runtime.ownership.poll_qr(data.get('challenge_id'), data.get('secret'), data.get('qr_session_id'))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return JSONResponse(result, headers=headers())
