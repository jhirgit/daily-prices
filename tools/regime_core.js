// regime_core.js — CANONICAL regime/rotation/rank-receipt engine for jr-dash.
//
// This is the single source of truth for the client-side regime math that the
// Technicals tab runs in the browser. The `RC` object inlined in index.html
// (the `var RC=(function(){...})();` IIFE) is a hand-copy of THIS logic; keeping
// two copies is a drift hazard, so this file exists to be (a) unit-tested in Node
// and (b) the file index.html sources RC from at the next deploy (see
// regime_core.README-wiring.md for the exact one-line change).
//
// The function bodies below are copied VERBATIM from index.html's inline RC
// (index.html v52, lines ~1447-1496). Do NOT "clean up" or refactor them here —
// any behavior change must stay logic-identical to the inline copy, and the Node
// test suite (test/test_regime.js) is the guardrail that proves it.
//
// v58 (jr-dash): sectorLadder additionally carries r21 (21-session ≈ 1-month return) and
// third21 (last-bar third of the field by 21d return) — the ladder's short-term
// trend read. Added ORACLE-FIRST (no inline index.html copy exists since #8
// Phase 3); daily-prices/regime.py ports it and the parity fixtures pin it.
//
// UMD-ish guard: works in Node (module.exports) AND the browser (window.RC).
//
// Semantics reference: TECHNICALS_SPEC.md §4-§6.  This is an EQUITY-INTERNAL /
// cross-asset risk-appetite read (thresholds fixed & economically motivated,
// never grid-fit); base rates are survivorship-caveated by construction.

(function (root, factory) {
  var RC = factory();
  if (typeof module !== "undefined" && module.exports) { module.exports = RC; } // Node / CommonJS
  if (root) { root.RC = RC; }                                                   // browser: window.RC
})(typeof self !== "undefined" ? self : (typeof globalThis !== "undefined" ? globalThis : this), function () {
  // ---- BEGIN verbatim RC internals (index.html lines ~1447-1496) ----
  function ema(arr,span){var out=new Array(arr.length),a=2/(span+1),prev=null;for(var i=0;i<arr.length;i++){var v=arr[i];if(v==null){out[i]=prev;continue;}prev=prev==null?v:a*v+(1-a)*prev;out[i]=prev;}return out;}
  function sma(arr,n){var out=new Array(arr.length),sum=0,q=[];for(var i=0;i<arr.length;i++){var v=arr[i];if(v==null){out[i]=null;continue;}q.push(v);sum+=v;if(q.length>n)sum-=q.shift();out[i]=q.length===n?sum/n:null;}return out;}
  function ratio(num,den){var out=new Array(num.length);for(var i=0;i<num.length;i++){out[i]=(num[i]==null||den[i]==null||den[i]===0)?null:num[i]/den[i];}return out;}
  function median(a){var b=a.filter(function(x){return x!=null;}).slice().sort(function(p,q){return p-q;});if(!b.length)return null;var m=Math.floor(b.length/2);return b.length%2?b[m]:(b[m-1]+b[m])/2;}
  function parseCsv(text){
    var lines=text.split(/\r?\n/),h=-1;
    for(var i=0;i<lines.length;i++){if(lines[i].trim()){h=i;break;}}
    if(h<0)return{dates:[],series:{}};
    var cols=lines[h].split(",").map(function(s){return s.trim().toLowerCase();});
    function fc(names){for(var k=0;k<names.length;k++){var idx=cols.indexOf(names[k]);if(idx>=0)return idx;}return -1;}
    var ci={date:fc(["date","day","dt"]),tk:fc(["ticker","symbol","sym"]),close:fc(["adj_close","adjclose","adjusted_close","adj close","close","c"])};
    if(ci.date<0||ci.tk<0||ci.close<0)throw new Error("csv columns not found");
    var byT={},ds={};
    for(var r=h+1;r<lines.length;r++){var ln=lines[r];if(!ln)continue;var f=ln.split(","),d=f[ci.date],t=f[ci.tk],c=parseFloat(f[ci.close]);if(!d||!t||!isFinite(c))continue;d=d.trim();t=t.trim().toUpperCase();(byT[t]||(byT[t]={}))[d]=c;ds[d]=1;}
    var dates=Object.keys(ds).sort(),series={};
    Object.keys(byT).forEach(function(t){var m=byT[t],arr=new Array(dates.length),prev=null,started=false;for(var j=0;j<dates.length;j++){var v=m[dates[j]];if(v!=null){prev=v;started=true;}arr[j]=started?prev:null;}series[t]=arr;});
    return{dates:dates,series:series};
  }
  function ratioLegSeries(rat){var e=ema(rat,50),out=new Array(rat.length);for(var i=0;i<rat.length;i++){var v=rat[i],ev=e[i],ep=i>=21?e[i-21]:null;if(v==null||ev==null||ep==null){out[i]=0;continue;}var rising=ev>ep,above=v>ev;out[i]=(above&&rising)?1:(!above&&!rising)?-1:0;}return out;}
  function trendLegSeries(px,invert){var e=ema(px,50),out=new Array(px.length),s=invert?-1:1;for(var i=0;i<px.length;i++){var v=px[i],ev=e[i],ep=i>=21?e[i-21]:null;if(v==null||ev==null||ep==null){out[i]=0;continue;}var rising=ev>ep,above=v>ev;out[i]=(above&&rising)?s:(!above&&!rising)?-s:0;}return out;}
  function breadthSeries(series,names){var above=names.map(function(t){var c=series[t];if(!c)return null;var d=sma(c,200),a=new Array(c.length);for(var i=0;i<c.length;i++)a[i]=(c[i]==null||d[i]==null)?null:(c[i]>d[i]?1:0);return a;}).filter(Boolean);var n=above.length?above[0].length:0,frac=new Array(n);for(var i=0;i<n;i++){var up=0,tot=0;for(var k=0;k<above.length;k++){var v=above[k][i];if(v!=null){tot++;up+=v;}}frac[i]=tot?up/tot:null;}return frac;}
  function breadthLegSeries(frac){return frac.map(function(f){return f==null?0:f>=0.55?1:f<=0.35?-1:0;});}
  function compositeSeries(legs){var n=legs[0].length,k=legs.length||1,thr=0.34,sum=new Array(n),state=new Array(n);for(var i=0;i<n;i++){var s=0;for(var j=0;j<legs.length;j++)s+=legs[j][i];sum[i]=s;var net=s/k;state[i]=net>=thr?"risk-on":net<=-thr?"defensive":"neutral";}return{sum:sum,state:state};}
  function flips(dates,state){var out=[],prev=null;for(var i=0;i<state.length;i++){if(state[i]!==prev&&prev!=null)out.push({date:dates[i],from:prev,to:state[i]});prev=state[i];}return out;}
  function baseRates(series,names,state,H){H=H||21;var arrs=names.map(function(t){return series[t];}).filter(Boolean);var n=arrs.length?arrs[0].length:0;var bk={"risk-on":[],"neutral":[],"defensive":[]};for(var t=0;t+H<n;t++){var st=state[t];if(!bk[st])continue;var rets=[];for(var k=0;k<arrs.length;k++){var a=arrs[k][t],b=arrs[k][t+H];if(a!=null&&b!=null&&a>0)rets.push(b/a-1);}var m=median(rets);if(m!=null)bk[st].push(m);}var res={};Object.keys(bk).forEach(function(s){var arr=bk[s];res[s]={n:arr.length,median:median(arr),hit:arr.length?arr.filter(function(x){return x>0;}).length/arr.length:null};});return res;}
  function mom121Series(close){var LONG=273,SHORT=21,out=new Array(close.length);for(var i=0;i<close.length;i++){var a=i>=LONG?close[i-LONG]:null,b=i>=SHORT?close[i-SHORT]:null;out[i]=(a!=null&&b!=null&&a>0)?b/a-1:null;}return out;}
  function rankReceipts(series,names){
    var present=names.filter(function(t){return series[t];});
    var mom=present.map(function(t){return mom121Series(series[t]);});
    var n=mom.length?mom[0].length:0,rank=present.map(function(){return new Array(n);});
    for(var i=0;i<n;i++){var vals=[];for(var k=0;k<present.length;k++){var v=mom[k][i];if(v!=null)vals.push({k:k,v:v});}vals.sort(function(p,q){return p.v-q.v;});var L=vals.length;for(var j=0;j<L;j++)rank[vals[j].k][i]=L>1?j/(L-1):0.5;}
    function tierAt(k,i){var r=rank[k][i];if(r==null)return null;return r>=0.8?"top":r<=0.2?"bottom":"mid";}
    var last=n-1,out={};
    for(var k2=0;k2<present.length;k2++){var t=present[k2],curTier=tierAt(k2,last),rec={ticker:t,rank:rank[k2][last]==null?null:rank[k2][last],tier:curTier,streak:0,sinceRet:null,entryIdx:null};if(curTier==="top"||curTier==="bottom"){var i2=last,s=0;while(i2>=0&&tierAt(k2,i2)===curTier){s++;i2--;}rec.streak=s;rec.entryIdx=i2+1;var c=series[t],pe=c[rec.entryIdx],pn=c[last];rec.sinceRet=(pe!=null&&pn!=null&&pe>0)?pn/pe-1:null;}out[t]=rec;}
    return out;
  }
  function ret(close,lag){var i=close.length-1,a=i>=lag?close[i-lag]:null,b=close[i];return(a!=null&&b!=null&&a>0)?b/a-1:null;}
  function sectorLadder(series,etfs){
    var present=etfs.filter(function(e){return series[e.t];});
    var rows=present.map(function(e){var c=series[e.t],r21=ret(c,21),r63=ret(c,63),r126=ret(c,126),blend=(r63!=null&&r126!=null)?(r63+r126)/2:(r63!=null?r63:r126);return{t:e.t,name:e.name,side:e.side||null,r21:r21,r63:r63,r126:r126,blend:blend};});
    var n=present.length?series[present[0].t].length:0;
    var r63ser=present.map(function(e){var c=series[e.t],s=new Array(c.length);for(var i=0;i<c.length;i++){var a=i>=63?c[i-63]:null,b=c[i];s[i]=(a!=null&&b!=null&&a>0)?b/a-1:null;}return s;});
    var r21ser=present.map(function(e){var c=series[e.t],s=new Array(c.length);for(var i=0;i<c.length;i++){var a=i>=21?c[i-21]:null,b=c[i];s[i]=(a!=null&&b!=null&&a>0)?b/a-1:null;}return s;});
    function thirdAt(k,i){var mine=r63ser[k][i];if(mine==null)return null;var vals=[];for(var q=0;q<present.length;q++){var v=r63ser[q][i];if(v!=null)vals.push(v);}vals.sort(function(p,z){return z-p;});var L=vals.length,pos=vals.indexOf(mine);if(pos<Math.ceil(L/3))return"top";if(pos>=L-Math.ceil(L/3))return"bottom";return"mid";}
    function third21At(k,i){var mine=r21ser[k][i];if(mine==null)return null;var vals=[];for(var q=0;q<present.length;q++){var v=r21ser[q][i];if(v!=null)vals.push(v);}vals.sort(function(p,z){return z-p;});var L=vals.length,pos=vals.indexOf(mine);if(pos<Math.ceil(L/3))return"top";if(pos>=L-Math.ceil(L/3))return"bottom";return"mid";}
    var last=n-1;
    rows.forEach(function(row,k){var tThird=thirdAt(k,last);row.third=tThird;row.third21=third21At(k,last);row.streak=0;if(tThird==="top"||tThird==="bottom"){var i=last,s=0;while(i>=0&&thirdAt(k,i)===tThird){s++;i--;}row.streak=s;}});
    rows.sort(function(a,b){return(b.blend==null?-9e9:b.blend)-(a.blend==null?-9e9:a.blend);});
    var topOff=rows.some(function(r){return r.third==="top"&&r.side==="offense";}),topDef=rows.some(function(r){return r.third==="top"&&r.side==="defense";});
    return{rows:rows,divergence:topOff&&topDef};
  }
  // ---- END verbatim RC internals ----

  // Exported surface — identical set/names to index.html's inline RC return.
  return {parseCsv:parseCsv,ratio:ratio,ratioLegSeries:ratioLegSeries,trendLegSeries:trendLegSeries,breadthSeries:breadthSeries,breadthLegSeries:breadthLegSeries,compositeSeries:compositeSeries,flips:flips,baseRates:baseRates,rankReceipts:rankReceipts,sectorLadder:sectorLadder,ret:ret};
});
