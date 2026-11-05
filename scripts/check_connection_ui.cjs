const fs=require('fs'),path=require('path'),crypto=require('crypto'),assert=require('assert'),{spawn}=require('child_process');
const {chromium}=require(process.env.COMMON_ROOM_PLAYWRIGHT||'playwright');
const root=path.resolve(__dirname,'..'),prefix=process.argv[2]||'connection-browser',receipt=path.join(root,'evidence',prefix+'.json');
if(fs.existsSync(receipt))throw Error('Preserve prior evidence');
const fixture=fs.mkdtempSync(path.join(root,'.checks','connection-browser-')),connection=path.join(fixture,'connection.json'),temp=path.join(fixture,'temp');fs.mkdirSync(temp);
const digest=b=>crypto.createHash('sha256').update(b).digest('hex'),hashes={};
for(const folder of ['common_room','web','scripts','tests'])for(const name of fs.readdirSync(path.join(root,folder)))if(/\.(py|cjs|js|html|css)$/.test(name))hashes[folder+'/'+name]=digest(fs.readFileSync(path.join(root,folder,name)));
const child=spawn(process.env.COMMON_ROOM_PYTHON||'python',['-B','scripts/connection_ui_fixture.py',connection],{cwd:root,env:{...process.env,TEMP:temp,TMP:temp,PYTHONDONTWRITEBYTECODE:'1'},stdio:['pipe','pipe','pipe']});
let browser,failure=null,checks=[],stage='startup',stderr='';child.stdout.on('data',()=>{});child.stderr.on('data',b=>stderr+=b);
(async()=>{try{
 for(let i=0;i<100&&!fs.existsSync(connection);i++)await new Promise(r=>setTimeout(r,100));
 assert(fs.existsSync(connection),stderr);const config=JSON.parse(fs.readFileSync(connection));
 browser=await chromium.launch({...(process.env.COMMON_ROOM_BROWSER?{executablePath:process.env.COMMON_ROOM_BROWSER}:{channel:'chrome'}),headless:true,env:{...process.env,TEMP:temp,TMP:temp},args:['--disable-background-networking']});
 const context=await browser.newContext({viewport:{width:1440,height:1000},serviceWorkers:'block'});
 await context.route('**/*',r=>r.request().url().startsWith(config.origin+'/')?r.continue():r.abort());
 const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(config.origin);await page.locator('#access-key').fill(config.key);await page.locator('#login-button').click();await page.locator('#workspace').waitFor({state:'visible'});
 stage='task';await page.locator('#task-title').fill('A connected browser round trip');await page.locator('#task-body').fill('Three fictional words');await page.locator('#task-submit').click();
 let card=page.locator('.card').filter({hasText:'A connected browser round trip'});await card.waitFor();assert.equal(await card.getByRole('button',{name:'Run worker',exact:true}).count(),0);
 await card.getByRole('button',{name:'Deliver',exact:true}).click();await card.getByRole('button',{name:'Run worker',exact:true}).waitFor();checks.push('Only delivered tasks offer an explicit run');
 stage='pause';await page.locator('#pause').click();await page.getByRole('button',{name:'Resume work',exact:true}).waitFor();assert.equal(await card.getByRole('button',{name:'Run worker',exact:true}).count(),0);await page.locator('#pause').click();await card.getByRole('button',{name:'Run worker',exact:true}).waitFor();checks.push('Pause removes run control and resume restores it');
 stage='worker';await page.route('**/api/run',async route=>{await route.fetch();await route.abort();},{times:1});await card.getByRole('button',{name:'Run worker',exact:true}).click();await page.getByRole('button',{name:'Recover pending run',exact:true}).waitFor({timeout:2000});await page.locator('#refresh').click();await card.getByRole('button',{name:'Run reviewer',exact:true}).waitFor({timeout:6000});await page.getByRole('button',{name:'Recover pending run',exact:true}).click();await page.getByRole('button',{name:'Recover pending run',exact:true}).waitFor({state:'hidden',timeout:3000});checks.push('Lost worker response recovered from stable control after task changed to Review');
 stage='reviewer';await card.getByRole('button',{name:'Run reviewer',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.card .task-status')?.textContent==='done',{timeout:6000});checks.push('Browser starts separate reviewer and shows Done');
 stage='receipts';await card.getByText('Durable event receipts',{exact:true}).click();assert((await card.innerText()).includes('reviewer'));assert((await page.locator('#connection-receipts').innerText()).includes('done'));assert((await page.locator('#fixture-label').innerText()).includes('no real model calls'));checks.push('Separate identity receipts and honest offline-test label');
 await page.locator('#note-body').fill('<img src=x onerror="window.bad=1">');await page.locator('#note-submit').click();await page.getByText('<img src=x onerror="window.bad=1">',{exact:true}).waitFor();assert.equal(await page.evaluate(()=>window.bad),undefined);checks.push('Lounge HTML stays inert');
 stage='reload';await page.reload();await page.locator('#workspace').waitFor({state:'visible'});await page.locator('.card .task-status').filter({hasText:/^done$/}).waitFor();assert((await page.locator('#connection-receipts').innerText()).includes('done'));checks.push('Reload preserves task and transport receipts');
 await page.screenshot({path:path.join(root,'evidence',prefix+'-desktop.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:path.join(root,'evidence',prefix+'-mobile.png'),fullPage:true});checks.push('390-pixel layout fits');assert.deepEqual(errors,[]);checks.push('No browser errors');
}catch(e){failure={stage,message:e.message};}
finally{if(browser)await browser.close();child.stdin.end('\n');await Promise.race([new Promise(r=>child.once('exit',r)),new Promise(r=>setTimeout(r,5000))]);if(child.exitCode===null)child.kill();}
const after={};for(const rel in hashes)after[rel]=digest(fs.readFileSync(path.join(root,rel)));
const result={checks,failure,source_hashes:hashes,source_unchanged:JSON.stringify(hashes)===JSON.stringify(after),clock_source:'Node Date system clock',finished_utc:new Date().toISOString(),fixture:fs.existsSync(connection.replace('.json','.result.json'))?JSON.parse(fs.readFileSync(connection.replace('.json','.result.json'))):null,child_stopped:child.exitCode!==null};
fs.writeFileSync(receipt,JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify({receipt,checks:checks.length,failure,child_stopped:result.child_stopped}));process.exitCode=failure?1:0;
})();
