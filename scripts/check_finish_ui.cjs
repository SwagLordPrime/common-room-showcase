// Real Chromium UI checks. Every process profile and fixture stays under .checks.
const fs=require('fs'),path=require('path'),crypto=require('crypto'),assert=require('assert'),{spawn}=require('child_process');
const {chromium}=require(process.env.COMMON_ROOM_PLAYWRIGHT || 'playwright');
const root=path.resolve(__dirname,'..'),phase=process.argv[2]||'green',prefix=process.argv[3]||(phase==='red'?'ui-red':'ui-results');
const evidence=path.join(root,'evidence'),receipt=path.join(evidence,prefix+'.json');
if(fs.existsSync(receipt))throw Error('Preserve evidence; choose a new prefix.');
const fixture=fs.mkdtempSync(path.join(root,'.checks','browser-')),connection=path.join(fixture,'connection.json');
const temp=path.join(fixture,'temp');fs.mkdirSync(temp);
const digest=b=>crypto.createHash('sha256').update(b).digest('hex');
const before={},snapshot=path.join(evidence,prefix+'-snapshot');fs.mkdirSync(snapshot);
for(const folder of ['common_room','tests','web','scripts'])for(const name of fs.readdirSync(path.join(root,folder))){if(!/\.(py|html|js|css|cjs)$/.test(name))continue;const rel=folder+'/'+name,b=fs.readFileSync(path.join(root,rel));before[rel]=digest(b);fs.mkdirSync(path.dirname(path.join(snapshot,rel)),{recursive:true});fs.writeFileSync(path.join(snapshot,rel),b);}
const python=process.env.COMMON_ROOM_PYTHON || 'python';
const child=spawn(python,['-B','-m','common_room.demo','--connection-file',connection],{cwd:root,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',TEMP:temp,TMP:temp},stdio:['pipe','pipe','pipe']});
let childError='';child.stderr.on('data',b=>childError+=b);child.stdout.on('data',()=>{});
let browser,stage='server_start',failure=null,checks=[],images=[];const start=new Date().toISOString();
(async()=>{try{
 for(let i=0;i<100&&!fs.existsSync(connection);i++)await new Promise(r=>setTimeout(r,100));
 if(!fs.existsSync(connection))throw Error('Fixture did not start: '+childError);
 const config=JSON.parse(fs.readFileSync(connection));stage='browser_launch';
 browser=await chromium.launch({...(process.env.COMMON_ROOM_BROWSER?{executablePath:process.env.COMMON_ROOM_BROWSER}:{channel:'chrome'}),headless:true,env:{...process.env,TEMP:temp,TMP:temp,TMPDIR:temp},args:['--disable-background-networking']});
 const context=await browser.newContext({viewport:{width:1440,height:1000},serviceWorkers:'block'});
 await context.route('**/*',route=>route.request().url().startsWith(config.origin+'/')?route.continue():route.abort());
 const page=await context.newPage();let errors=[];page.on('pageerror',e=>errors.push(e.message));
 stage='page_load';assert.equal((await page.goto(config.origin)).status(),200);checks.push('Actual loopback page loaded');
 stage='login_controls';await page.locator('#access-key').waitFor({timeout:1500});
 await page.locator('#access-key').fill(config.key);await page.locator('#login-button').click();await page.locator('#workspace').waitFor({state:'visible',timeout:5000});
 checks.push('Authenticated browser session');stage='filters';await page.locator('#search').waitFor({timeout:1500});await page.locator('#search').fill('Count the orchard');assert.equal(await page.locator('.card').count(),1);await page.locator('#search').fill('');stage='board_states';assert.equal(await page.locator('[data-column]').count(),5);
 for(const name of ['Inbox','Working','Review','Done','Owner decisions'])assert(await page.getByRole('heading',{name,exact:true}).isVisible());
 assert.equal(await page.locator('.card').count(),5);checks.push('All five lifecycle columns reflect prepared store tasks');
 stage='combined_filters';await page.locator('#filter-status').selectOption('done');assert.equal(await page.locator('.card').count(),1);await page.locator('#filter-recipient').selectOption('scribe');await page.locator('#filter-scope').selectOption('demo');assert.equal(await page.locator('.card').count(),1);await page.locator('#needs-shane').click();assert.equal(await page.locator('.card').count(),1);assert((await page.locator('.card').innerText()).includes('Which fictional note'));await page.locator('#clear-filters').click();assert.equal(await page.locator('.card').count(),5);checks.push('Combined filters and actual Needs Shane reason');
 const decisionCard=page.locator('.card').filter({hasText:'A question for the Keeper'});page.once('dialog',dialog=>dialog.accept('Use the current fictional note'));await decisionCard.getByRole('button',{name:'Record decision'}).click();await page.getByText('Recorded decision: Use the current fictional note',{exact:true}).waitFor();await decisionCard.getByText('Durable event receipts',{exact:true}).click();assert((await decisionCard.innerText()).includes('Which fictional note'));assert(await decisionCard.locator('code.digest').count()>0);checks.push('Recorded decision and durable event hashes remain visible');

 assert((await page.locator('#participants').innerText()).includes('No fixture contact'));assert((await page.locator('#fixture-label').innerText()).includes('No real assistants'));checks.push('Fixture and offline labels visible');
 stage='create';await page.locator('#task-title').fill('Browser-created quest');await page.locator('#task-body').fill('Three words here');await page.locator('#task-submit').click();
 const card=page.locator('.card').filter({hasText:'Browser-created quest'});await card.waitFor();await card.getByRole('button',{name:'Deliver',exact:true}).click();
 await page.waitForFunction(()=>[...document.querySelectorAll('.card')].some(c=>c.textContent.includes('Browser-created quest')&&c.querySelector('.task-status').textContent==='delivered'));
 checks.push('Browser creates and delivers through the store');
 stage='hostile_content';const hostile='<img src=x onerror="window.roomXss=1">';await page.locator('#note-body').fill(hostile);await page.locator('#note-submit').click();await page.getByText(hostile,{exact:true}).waitFor();
 assert.equal(await page.evaluate(()=>window.roomXss),undefined);assert.equal(await page.locator('#notes img').count(),0);checks.push('Hostile HTML rendered as text');
 stage='screenshots';const desktop=prefix==='ui-results'?'board-preview.png':prefix+'-desktop.png';await page.screenshot({path:path.join(evidence,desktop),fullPage:true});images.push(desktop);
 await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 assert(await page.locator('#refresh').isVisible());const mobile=prefix+'-mobile.png';await page.screenshot({path:path.join(evidence,mobile),fullPage:true});images.push(mobile);checks.push('Phone layout has no horizontal overflow');
 stage='reload_logout';await page.reload();await page.locator('#workspace').waitFor({state:'visible'});await page.locator('.card').filter({hasText:'Browser-created quest'}).waitFor();
 await page.locator('#logout').click();await page.locator('#login-panel').waitFor({state:'visible'});assert.equal((await context.request.get(config.origin+'/api/state')).status(),401);checks.push('Session reload and server-side logout');
 stage='disconnection';await page.reload();await page.locator('#access-key').fill(config.key);await page.locator('#login-button').click();await page.locator('#workspace').waitFor({state:'visible'});await page.route('**/api/state',route=>route.abort());await page.locator('#refresh').click();await page.getByText('Disconnected · last successful refresh is stale',{exact:true}).waitFor();checks.push('Lost connection labels retained view honestly');assert.deepEqual(errors,[]);checks.push('No browser script errors');
}catch(e){failure={stage,name:e.name,message:e.message};}
finally{if(browser)await browser.close();child.stdin.end('\n');await Promise.race([new Promise(r=>child.once('exit',r)),new Promise(r=>setTimeout(r,5000))]);if(child.exitCode===null)child.kill();}
const after={};for(const rel of Object.keys(before))after[rel]=digest(fs.readFileSync(path.join(root,rel)));
const observed=phase==='red'?failure?.stage==='filters':failure===null;
const record={gate:'CR-BOARD',phase,started_utc:start,finished_utc:new Date().toISOString(),clock_source:'Node Date system clock; America/Chicago CDT UTC-05:00',checks,failure,expected_result_observed:observed,source_hashes_before:before,source_hashes_after:after,source_unchanged:JSON.stringify(before)===JSON.stringify(after),screenshots:images.map(n=>({path:'evidence/'+n,sha256:digest(fs.readFileSync(path.join(evidence,n)))})),fixture:'Local prepared data only; no provider connection or model calls',child_stopped:child.exitCode!==null};
fs.writeFileSync(receipt,JSON.stringify(record,null,2)+'\n');console.log(JSON.stringify({receipt,phase,checks:checks.length,failure,expected_result_observed:observed,child_stopped:record.child_stopped}));process.exitCode=observed?0:1;
})();
