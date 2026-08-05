#!/usr/bin/env python3
"""Send de-duplicated competitor alerts in a wide executive email."""
from __future__ import annotations
import base64, html, json, os, re, smtplib, ssl, urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
NEWS=ROOT/'data/news.json'; STATE=ROOT/'data/notified_ids.json'; STATUS=ROOT/'data/email_status.json'; LOGOS=ROOT/'assets/email-logos'
DASH='https://atanubarik.github.io/laboratory-news-monitor/'
WORKER=os.getenv('AI_WORKER_URL','https://laboratory-news-ai.atanu-barik.workers.dev').strip()
IST=ZoneInfo('Asia/Kolkata'); PAC=ZoneInfo('America/Los_Angeles'); MAX_ITEMS=40
COMPANIES=['Labcorp','Quest Diagnostics','ARUP Laboratories','Mayo Clinic Laboratories','Sonic Healthcare']
CATEGORIES=['Product & Services','Clinical, R&D','Partnership, M&A','Financials','Organizational Updates','Leadership Changes','Other']
LOGO_FILES={'Labcorp':'labcorp.jpg.b64','Quest Diagnostics':'quest-diagnostics.jpg.b64','ARUP Laboratories':'arup-laboratories.jpg.b64','Mayo Clinic Laboratories':'mayo-clinic-laboratories.jpg.b64','Sonic Healthcare':'sonic-healthcare.jpg.b64'}
CIDS={'Labcorp':'logo-labcorp','Quest Diagnostics':'logo-quest','ARUP Laboratories':'logo-arup','Mayo Clinic Laboratories':'logo-mayo','Sonic Healthcare':'logo-sonic'}
ACCENTS={'Labcorp':'#25A9E0','Quest Diagnostics':'#005A2B','ARUP Laboratories':'#A6192E','Mayo Clinic Laboratories':'#005EB8','Sonic Healthcare':'#00599C'}
BAD_SUMMARY=('insufficient','unable to read','cannot access','not enough information','repository evidence','no relevant information','source article should be reviewed')


def load(path, default):
    try:return json.loads(path.read_text(encoding='utf-8'))
    except Exception:return default

def save(path,payload):path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(payload,indent=2,ensure_ascii=False),encoding='utf-8')
def env_list(name):return list(dict.fromkeys(x.strip() for x in os.getenv(name,'').split(',') if x.strip()))
def truthy(name):return os.getenv(name,'').lower() in {'1','true','yes','on'}
def item_id(x):return str(x.get('id') or '').strip()
def clean_title(x):return re.sub(r'\s+',' ',str(x.get('title') or 'Untitled update')).strip()
def source_links(x):
    out=[];seen=set()
    for s in x.get('sources') or [{'name':x.get('source'),'url':x.get('url')}]:
        u=str(s.get('url') or '').strip(); n=str(s.get('name') or 'Source').strip()
        if u and u not in seen:seen.add(u);out.append((n,u))
    return out

def request_summary(item):
    prompt=f'''Read the underlying public reporting for this update and return only a concise 55-85 word business-intelligence summary, with no heading or bullets.
Explain what happened, the critical facts, and why it matters. Use plain language. Do not repeat the headline, publisher, date, ticker, legal suffix, or category.
For Financials include reported revenue, growth, profit/earnings, margins, guidance and segment details when available.
For Partnership, M&A include the parties, value/terms, conditions/timing, assets/capabilities and strategic rationale when available.
For Product & Services include the product/service, indication/use case, approval/status, customers/geography and differentiation.
For Clinical, R&D include study design, population, endpoints, quantitative findings and implications.
For Organizational Updates or Leadership Changes include the exact change, scope, timing and expected impact.
If the article cannot be verified or lacks enough detail, return exactly SKIP.
Update: {clean_title(item)}'''
    body=json.dumps({'mode':'chat','question':prompt,'article_ids':[item_id(item)],'filters':{'email_digest':'true'}}).encode()
    req=urllib.request.Request(WORKER,data=body,method='POST',headers={'Content-Type':'application/json','Accept':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=120) as r: ans=' '.join(str(json.loads(r.read().decode()).get('answer') or '').split())
    except Exception:return ''
    if not ans or ans.upper()=='SKIP' or any(p in ans.lower() for p in BAD_SUMMARY):return ''
    return ans

def summaries(items):
    out={}
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs={pool.submit(request_summary,x):item_id(x) for x in items}
        for f in as_completed(jobs):
            s=f.result()
            if s:out[jobs[f]]=s
    return out

def grouped(items):
    g=defaultdict(lambda:defaultdict(list))
    for x in items:g[str(x.get('company') or 'Other')][str(x.get('category') or 'Other')].append(x)
    return g

def logo_html(company,width=110):return f'<img src="cid:{CIDS[company]}" width="{width}" alt="{html.escape(company)}" style="display:block;max-width:{width}px;height:auto;border:0;object-fit:contain;">'
def breakdown_html(items):
    counts=Counter(str(x.get('company') or 'Other') for x in items); cells=[]
    for c in COMPANIES:
        if not counts.get(c):continue
        w=150 if c=='Labcorp' else 105
        cells.append(f'''<td align="center" valign="middle" style="padding:10px 14px;border-right:1px solid #E5E7EB;">{logo_html(c,w)}<div style="margin-top:7px;font:700 18px Arial;color:{ACCENTS[c]};">{counts[c]} <span style="font:400 11px Arial;color:#6B7280;">alerts</span></div></td>''')
    return '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid #E5E7EB;border-radius:12px;"><tr>'+''.join(cells)+'</tr></table>'
def coverage_html(item):
    links=source_links(item)
    if len(links)<2:return ''
    return '<div style="margin-top:7px;font:11px Arial;color:#6B7280;">Additional coverage: '+' | '.join(f'<a href="{html.escape(u,quote=True)}" style="color:#6B7280;">{html.escape(n)}</a>' for n,u in links[1:])+'</div>'
def article_html(item,summary):
    links=source_links(item); url=links[0][1] if links else str(item.get('url') or '')
    title=html.escape(clean_title(item)); title=f'<a href="{html.escape(url,quote=True)}" style="color:#006A58;text-decoration:underline;font-weight:700;">{title}</a>' if url else title
    desc=f'<div style="margin-top:8px;font:14px/21px Arial;color:#4B5563;">{html.escape(summary)}</div>' if summary else ''
    return f'<tr><td style="padding:0 0 18px 0;font:16px/23px Arial;">{title}{desc}{coverage_html(item)}</td></tr>'
def news_html(items,sums):
    g=grouped(items); blocks=[]
    for c in COMPANIES:
        if c not in g:continue
        cats=[]
        for cat in CATEGORIES:
            if cat not in g[c]:continue
            rows=''.join(article_html(x,sums.get(item_id(x),'')) for x in g[c][cat])
            cats.append(f'<tr><td style="padding:10px 14px;background:#F1F6F5;border-left:4px solid {ACCENTS[c]};font:800 13px Arial;color:#374151;text-transform:uppercase;">{html.escape(cat)}</td></tr><tr><td style="padding:16px 18px 2px;"><table role="presentation" width="100%">{rows}</table></td></tr>')
        count=sum(len(v) for v in g[c].values()); w=190 if c=='Labcorp' else 145
        blocks.append(f'''<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;background:#fff;border:1px solid #DDE5E3;border-radius:14px;"><tr><td style="padding:18px 22px;border-bottom:1px solid #E5E7EB;">{logo_html(c,w)}</td><td align="right" style="padding:18px 22px;border-bottom:1px solid #E5E7EB;font:12px Arial;color:#6B7280;">{count} alerts</td></tr>{''.join(cats)}</table>''')
    return ''.join(blocks)
def timing(now):
    ni=now.astimezone(IST); np=now.astimezone(PAC); nxt=now+timedelta(hours=6)
    fmt=lambda d:d.strftime('%I:%M %p').lstrip('0')
    return ni.strftime('%b %d, %Y'),fmt(ni),fmt(np),fmt(nxt.astimezone(IST)),fmt(nxt.astimezone(PAC))
def build(items,sums,is_test,now):
    date,ti,tp,nti,ntp=timing(now); subject=f'Quest Competitor Updates | {date}'
    body=f'''<!doctype html><html><body style="margin:0;background:#EDF3F2;"><table role="presentation" width="100%"><tr><td align="center" style="padding:24px 10px;"><table role="presentation" width="100%" style="max-width:1100px;"><tr><td style="padding:28px 32px;background:#073B3A;border-radius:16px 16px 0 0;color:#fff;"><div style="font:700 12px Arial;letter-spacing:1px;color:#9FE3D5;">QUEST BUSINESS INTELLIGENCE</div><div style="margin-top:8px;font:800 30px Arial;">Quest Business Intelligence Alerts: {len(items)} alerts</div><div style="margin-top:17px;font:700 13px Arial;">{date} &nbsp;|&nbsp; {ti} (IST) &nbsp;|&nbsp; {tp} (PTC)</div><div style="margin-top:7px;font:italic 10px/15px Arial;color:#CBE6E1;">Next Update will come by {nti} (IST) or {ntp} (PTC).</div></td></tr><tr><td style="padding:22px;background:#F8FAFA;border-left:1px solid #DDE5E3;border-right:1px solid #DDE5E3;"><div style="margin-bottom:12px;font:800 12px Arial;color:#374151;">COMPANY-WISE BREAKDOWN</div>{breakdown_html(items)}<div style="margin-top:13px;"><a href="{DASH}" style="font:700 12px Arial;color:#006A58;">Open the live dashboard and AI assistant</a></div></td></tr><tr><td style="padding:24px;background:#F8FAFA;border:1px solid #DDE5E3;border-top:0;border-radius:0 0 16px 16px;">{news_html(items,sums)}<div style="font:11px/16px Arial;color:#6B7280;">Summaries are AI-assisted. Review linked reporting before material decisions.</div></td></tr></table></td></tr></table></body></html>'''
    plain='\n'.join([subject,f'{date} | {ti} (IST) | {tp} (PTC)',f'Next update: {nti} IST / {ntp} PTC','',*[f'{clean_title(x)}\n{sums.get(item_id(x),"")}\n{source_links(x)[0][1] if source_links(x) else ""}' for x in items]])
    return subject,plain,body

def attach_logos(msg):
    alt=msg.get_payload()[-1]
    for c,f in LOGO_FILES.items():
        try:data=base64.b64decode((LOGOS/f).read_text().strip())
        except Exception:continue
        alt.add_related(data,maintype='image',subtype='jpeg',cid=f'<{CIDS[c]}>',filename=f.replace('.b64',''))
def send(subject,plain,body,to,bcc,from_addr,name):
    msg=EmailMessage();msg['Subject']=subject;msg['From']=formataddr((name,from_addr));msg['To']=', '.join(to);msg['Reply-To']=from_addr
    if bcc:msg['Bcc']=', '.join(bcc)
    msg.set_content(plain);msg.add_alternative(body,subtype='html');attach_logos(msg)
    host=os.getenv('SMTP_HOST','smtp.gmail.com');port=int(os.getenv('SMTP_PORT','465'));user=os.getenv('SMTP_USERNAME','');pwd=os.getenv('SMTP_APP_PASSWORD','');sec=os.getenv('SMTP_SECURITY','ssl')
    ctx=ssl.create_default_context()
    if sec=='starttls':
        with smtplib.SMTP(host,port,timeout=45) as s:s.starttls(context=ctx);s.login(user,pwd);s.send_message(msg)
    else:
        with smtplib.SMTP_SSL(host,port,context=ctx,timeout=45) as s:s.login(user,pwd);s.send_message(msg)
def main():
    repo=load(NEWS,{});items=sorted(repo.get('items') or [],key=lambda x:str(x.get('published_at') or ''),reverse=True);current={item_id(x) for x in items if item_id(x)}
    state_exists=STATE.exists();state=load(STATE,{'notified_ids':[]});known=set(state.get('notified_ids') or [])
    if not state_exists:state={'initialized_at':datetime.now(timezone.utc).isoformat(),'notified_ids':sorted(current)};save(STATE,state);known=current
    test=truthy('SEND_TEST_EMAIL');chosen=items[:5] if test else [x for x in items if item_id(x) not in known][:MAX_ITEMS]
    if not chosen:save(STATUS,{'checked_at':datetime.now(timezone.utc).isoformat(),'status':'no_new_items'});return 0
    to=env_list('EMAIL_TO');bcc=[x for x in env_list('EMAIL_BCC') if x.lower() not in {y.lower() for y in to}];from_addr=os.getenv('EMAIL_FROM') or os.getenv('SMTP_USERNAME','');name=os.getenv('EMAIL_SENDER_NAME','Quest Updates')
    sums=summaries(chosen);subject,plain,body=build(chosen,sums,test,datetime.now(timezone.utc));send(subject,plain,body,to,bcc,from_addr,name)
    if not test:known.update(item_id(x) for x in chosen);state['notified_ids']=sorted(known);state['last_successful_email_at']=datetime.now(timezone.utc).isoformat();save(STATE,state)
    save(STATUS,{'checked_at':datetime.now(timezone.utc).isoformat(),'status':'test_email_sent' if test else 'email_sent','email_item_count':len(chosen),'summary_count':len(sums),'to_count':len(to),'bcc_count':len(bcc),'subject':subject})
    print(f'Sent {len(chosen)} alerts with {len(sums)} summaries.')
    return 0
if __name__=='__main__':raise SystemExit(main())
