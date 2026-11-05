// Screenshots of the board for design review: signed out, signed in as the owner (desktop and
// phone, dark and light), a reviewer's view, and the public status page. Fictional room only.
// Usage: COMMON_ROOM_BROWSER=<chromium> node scripts/ui_preview.cjs <output folder>
const fs=require('fs'),path=require('path'),{spawn}=require('child_process');
const {chromium}=require(process.env.COMMON_ROOM_PLAYWRIGHT||'playwright');
const root=path.resolve(__dirname,'..'),out=path.resolve(process.argv[2]||path.join(root,'.checks','ui-preview-shots'));
fs.mkdirSync(out,{recursive:true});fs.mkdirSync(path.join(root,'.checks'),{recursive:true});
const conn=path.join(fs.mkdtempSync(path.join(root,'.checks','ui-preview-conn-')),'connection.json');
const child=spawn(process.env.COMMON_ROOM_PYTHON||'python3.12',['-B','scripts/ui_preview_fixture.py',conn],{cwd:root,stdio:['ignore','ignore','pipe']});
let err='';child.stderr.on('data',b=>err+=b);
(async()=>{let browser;const shots=[];try{
 for(let i=0;i<150&&!fs.existsSync(conn);i++)await new Promise(r=>setTimeout(r,100));
 if(!fs.existsSync(conn))throw Error('fixture did not start: '+err);
 const config=JSON.parse(fs.readFileSync(conn));
 browser=await chromium.launch({...(process.env.COMMON_ROOM_BROWSER?{executablePath:process.env.COMMON_ROOM_BROWSER}:{}),headless:true});
 const errors=[];
 async function view(scheme,viewport,key,name,after){
  const context=await browser.newContext({viewport,colorScheme:scheme,deviceScaleFactor:1});
  const page=await context.newPage();page.on('pageerror',e=>errors.push(name+': '+e.message));page.on('console',m=>{if(m.type()==='error')errors.push(name+': '+m.text());});
  await page.goto(config.origin+'/');
  if(key){await page.fill('#access-key',key);await page.click('#login-button');await page.waitForSelector('#workspace',{state:'visible'});await page.waitForTimeout(400);}
  if(after)await after(page);
  const overflow=await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth);
  const file=path.join(out,name+'.png');await page.screenshot({path:file,fullPage:true});shots.push({name,overflow});await context.close();
 }
 await view('dark',{width:1440,height:900},null,'gate-dark');
 await view('light',{width:1440,height:900},null,'gate-light');
 await view('dark',{width:1600,height:1000},config.key,'owner-dark');
 await view('light',{width:1600,height:1000},config.key,'owner-light');
 await view('dark',{width:390,height:844},config.key,'owner-phone-dark');
 await view('dark',{width:1100,height:900},config.reviewer_key,'reviewer-tablet-dark');
 await view('dark',{width:1440,height:900},null,'status-dark',async p=>{await p.goto(config.origin+'/status');await p.waitForTimeout(400);});
 console.log(JSON.stringify({out,shots,errors}));
}catch(e){console.error(e.stack||String(e));process.exitCode=1;}finally{if(browser)await browser.close();child.kill('SIGINT');}})();
