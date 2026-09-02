// regime_core.js — CANONICAL regime/rotation/rank-receipt engine for jr-dash.
//
// This is the single source of truth for the client-side regime math that the
// Technicals tab runs in the browser. The `RC` object inlined in index.html
// (the `var RC=(function(){...})();` IIFE) is a hand-copy of THIS logic; keeping
// two copies is a drift hazard, so this file exists to be (a) unit-tested in Node
// and (b) the file index.html sources RC from at the next deploy (see
// regime_core.README-wiring.md for the exact one-line change).
//
// v60 (jr-dash): framework-review upgrades, oracle-first — compositeSeries gains
// HYSTERESIS (enter ±0.34 / hold ±0.17) and a per-bar active-leg denominator k;
// new legs' primitives (retSeries/rvSeries/pctRankSeries/pctVoteSeries/
// corrPairSeries/corrVoteSeries/avgCorrSeries), durations, baseRatesMulti
// (5/21/63d + unconditional + n_eff), legFirstActive/firstNonNull, and
// sectorLadder grp-dedup (twins carry levels only + twin_of). regime.py ports
// all of it; the parity fixtures pin every function.
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
  // v60: hysteresis (enter |net|>=0.34, hold |net|>=0.17, opposite entry flips
  // direct) + per-bar active-leg denominator k(i) (a leg in warm-up no longer
  // dilutes toward neutral). firstActives optional (default: all 0).
  function compositeSeries(legs,firstActives){var n=legs[0].length,enter=0.34,exit=0.17,fas=firstActives||legs.map(function(){return 0;}),sum=new Array(n),kser=new Array(n),state=new Array(n),prev=null;for(var i=0;i<n;i++){var s=0,k=0;for(var j=0;j<legs.length;j++){s+=legs[j][i];if(fas[j]<=i)k++;}sum[i]=s;kser[i]=k;var net=k?s/k:0;var st;if(prev==="risk-on"){st=net<=-enter?"defensive":net>=exit?"risk-on":"neutral";}else if(prev==="defensive"){st=net>=enter?"risk-on":net<=-exit?"defensive":"neutral";}else{st=net>=enter?"risk-on":net<=-enter?"defensive":"neutral";}state[i]=st;prev=st;}return{sum:sum,state:state,k:kser};}
  function durations(state){var by={"risk-on":[],"neutral":[],"defensive":[]},cur=null,cnt=0;for(var i=0;i<state.length;i++){var s=state[i];if(s===cur){cnt++;}else{if(cur!=null&&by[cur])by[cur].push(cnt);cur=s;cnt=1;}}if(cur!=null&&by[cur])by[cur].push(cnt);var out={};["risk-on","neutral","defensive"].forEach(function(s2){out[s2]={n:by[s2].length,median:median(by[s2])};});return{by_state:out,current:cur==null?null:{state:cur,age:cnt}};}
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
  function retSeries(close){var out=new Array(close.length);for(var i=1;i<close.length;i++){var a=close[i-1],b=close[i];out[i]=(a!=null&&b!=null&&a>0)?b/a-1:null;}return out;}
  function rvSeries(close,n){n=n==null?21:n;var r=retSeries(close),out=new Array(close.length);for(var i=0;i<close.length;i++){if(i<n)continue;var s=0,s2=0,ok=true;for(var t=i-n+1;t<=i;t++){var v=r[t];if(v==null){ok=false;break;}s+=v;s2+=v*v;}if(!ok)continue;var va=(s2-s*s/n)/(n-1);if(va<0)va=0;out[i]=Math.sqrt(va)*Math.sqrt(252);}return out;}
  function pctRankSeries(vals,win){win=win==null?252:win;var out=new Array(vals.length);for(var i=0;i<vals.length;i++){if(i<win-1)continue;var v=vals[i];if(v==null)continue;var c=0,ok=true;for(var t=i-win+1;t<=i;t++){var w=vals[t];if(w==null){ok=false;break;}if(w<=v)c++;}if(ok)out[i]=c/win;}return out;}
  function pctVoteSeries(pct,lo,hi){lo=lo==null?0.30:lo;hi=hi==null?0.70:hi;var out=new Array(pct.length);for(var i=0;i<pct.length;i++){var p=pct[i];out[i]=p==null?0:p<=lo?1:p>=hi?-1:0;}return out;}  // for-loop, not .map: rv/pct arrays are sparse (holes), and .map skips holes
  function corrPairSeries(a,b,win){win=win==null?63:win;var ra=retSeries(a),rb=retSeries(b),n=Math.min(a.length,b.length),out=new Array(n);for(var i=0;i<n;i++){if(i<win)continue;var sa=0,sb=0,saa=0,sbb=0,sab=0,ok=true;for(var t=i-win+1;t<=i;t++){var x=ra[t],y=rb[t];if(x==null||y==null){ok=false;break;}sa+=x;sb+=y;saa+=x*x;sbb+=y*y;sab+=x*y;}if(!ok)continue;var cov=sab-sa*sb/win,va=saa-sa*sa/win,vb=sbb-sb*sb/win;if(va<=0||vb<=0)continue;out[i]=cov/Math.sqrt(va*vb);}return out;}
  function corrVoteSeries(corr,thr){thr=thr==null?0.20:thr;var out=new Array(corr.length);for(var i=0;i<corr.length;i++){var c=corr[i];out[i]=c==null?0:c<=-thr?1:c>=thr?-1:0;}return out;}  // for-loop, not .map: corrPairSeries output is sparse (holes)
  function avgCorrSeries(series,names,win,minN){win=win==null?63:win;minN=minN==null?8:minN;var rets=[];for(var q=0;q<names.length;q++){var c=series[names[q]];if(c)rets.push(retSeries(c));}var n=rets.length?rets[0].length:0,out=new Array(n);for(var i=0;i<n;i++){if(i<win)continue;var members=[];for(var k=0;k<rets.length;k++){var ok=true;for(var t=i-win+1;t<=i;t++){if(rets[k][t]==null){ok=false;break;}}if(ok)members.push(k);}var N=members.length;if(N<minN)continue;var s1=0,s2=0,psum=0,psum2=0;for(var t2=i-win+1;t2<=i;t2++){var p=0;for(var m=0;m<N;m++)p+=rets[members[m]][t2];p/=N;psum+=p;psum2+=p*p;}for(var m2=0;m2<N;m2++){var ss=0,sss=0;for(var t3=i-win+1;t3<=i;t3++){var v=rets[members[m2]][t3];ss+=v;sss+=v*v;}var va2=(sss-ss*ss/win)/(win-1);if(va2<0)va2=0;s1+=Math.sqrt(va2);s2+=va2;}var varp=(psum2-psum*psum/win)/(win-1),denom=s1*s1-s2;if(denom<=0)continue;out[i]=(N*N*varp-s2)/denom;}return out;}
  function legFirstActive(cont){var e=ema(cont,50);for(var i=0;i<cont.length;i++){if(i>=21&&cont[i]!=null&&e[i]!=null&&e[i-21]!=null)return i;}return cont.length;}
  function firstNonNull(arr){for(var i=0;i<arr.length;i++){if(arr[i]!=null)return i;}return arr.length;}
  function baseRatesMulti(series,names,state,hs){hs=hs||[5,21,63];var arrs=names.map(function(t){return series[t];}).filter(Boolean);var n=arrs.length?arrs[0].length:0,res={};for(var h=0;h<hs.length;h++){var H=hs[h],bk={"risk-on":[],"neutral":[],"defensive":[],"all":[]};for(var t=0;t+H<n;t++){var st=state[t],rets=[];for(var k=0;k<arrs.length;k++){var a=arrs[k][t],b=arrs[k][t+H];if(a!=null&&b!=null&&a>0)rets.push(b/a-1);}var m=median(rets);if(m!=null){if(bk[st])bk[st].push(m);bk["all"].push(m);}}var out={};["risk-on","neutral","defensive","all"].forEach(function(s2){var arr=bk[s2];out[s2]={n:arr.length,n_eff:arr.length?Math.ceil(arr.length/H):0,median:median(arr),hit:arr.length?arr.filter(function(x){return x>0;}).length/arr.length:null};});res["h"+H]=out;}return res;}
  // Stream D (9/2/26): equal-weight daily-rebalanced basket index (base 100) over the
  // members present -- mean of the members' simple returns each session, a member
  // without both bars sits out that day, a day with no member data carries the level.
  // regime.basket_series is the port; summation order (members order) is part of parity.
  function basketSeries(series,members){var cs=[];for(var m=0;m<members.length;m++){if(series[members[m]])cs.push(series[members[m]]);}if(!cs.length)return null;var n=cs[0].length,out=new Array(n),v=null;for(var i=0;i<n;i++){var acc=0,cnt=0;if(i>=1){for(var k=0;k<cs.length;k++){var c=cs[k];if(i<c.length){var a=c[i-1],b=c[i];if(a!=null&&b!=null&&a>0){acc+=b/a-1;cnt++;}}}}if(cnt){v=(v==null?100:v)*(1+acc/cnt);}out[i]=v;}return out;}
  function sectorLadder(series,etfs){
    var ser={};etfs.forEach(function(e){ser[e.t]=e.basket?basketSeries(series,e.basket):series[e.t];});series=ser;
    var present=etfs.filter(function(e){return series[e.t];});
    var seenGrp={},fieldSet={},twinOf=new Array(present.length).fill(null);
    for(var kk=0;kk<present.length;kk++){var g=present[kk].grp;if(!g){fieldSet[kk]=1;}else if(!(g in seenGrp)){seenGrp[g]=kk;fieldSet[kk]=1;}else{twinOf[kk]=present[seenGrp[g]].t;}}
    var fieldIdx=Object.keys(fieldSet).map(Number).sort(function(a,b){return a-b;});
    var rows=present.map(function(e,k){var c=series[e.t],r5=ret(c,5),r21=ret(c,21),r63=ret(c,63),r126=ret(c,126),blend=(r63!=null&&r126!=null)?(r63+r126)/2:(r63!=null?r63:r126);return{t:e.t,name:e.name,side:e.side||null,r5:r5,r21:r21,r63:r63,r126:r126,blend:blend,twin_of:twinOf[k],sector:e.sector||null,gics:e.gics||null,basket:!!e.basket,members:e.basket||null};});
    var n=present.length?series[present[0].t].length:0;
    function retSer(k,lag){var c=series[present[k].t],s=new Array(c.length);for(var i=0;i<c.length;i++){var a=i>=lag?c[i-lag]:null,b=c[i];s[i]=(a!=null&&b!=null&&a>0)?b/a-1:null;}return s;}
    var r63ser={},r21ser={};fieldIdx.forEach(function(k){r63ser[k]=retSer(k,63);r21ser[k]=retSer(k,21);});
    function thirdAt(ser,k,i){var mine=ser[k][i];if(mine==null)return null;var vals=[];for(var q=0;q<fieldIdx.length;q++){var v=ser[fieldIdx[q]][i];if(v!=null)vals.push(v);}vals.sort(function(p,z){return z-p;});var L=vals.length,pos=vals.indexOf(mine);if(pos<Math.ceil(L/3))return"top";if(pos>=L-Math.ceil(L/3))return"bottom";return"mid";}
    var last=n-1;
    rows.forEach(function(row,k){
      if(!(k in fieldSet)){row.third=null;row.third21=null;row.streak=0;return;}
      var tThird=thirdAt(r63ser,k,last);row.third=tThird;row.third21=thirdAt(r21ser,k,last);row.streak=0;
      if(tThird==="top"||tThird==="bottom"){var i=last,s=0;while(i>=0&&thirdAt(r63ser,k,i)===tThird){s++;i--;}row.streak=s;}
    });
    rows.sort(function(a,b){return(b.blend==null?-9e9:b.blend)-(a.blend==null?-9e9:a.blend);});
    var topOff=rows.some(function(r){return r.third==="top"&&r.side==="offense";}),topDef=rows.some(function(r){return r.third==="top"&&r.side==="defense";});
    return{rows:rows,divergence:topOff&&topDef};
  }
  // ---- END verbatim RC internals ----

  // Exported surface — identical set/names to index.html's inline RC return.
  return {parseCsv:parseCsv,ratio:ratio,ratioLegSeries:ratioLegSeries,trendLegSeries:trendLegSeries,breadthSeries:breadthSeries,breadthLegSeries:breadthLegSeries,compositeSeries:compositeSeries,durations:durations,flips:flips,baseRates:baseRates,baseRatesMulti:baseRatesMulti,rankReceipts:rankReceipts,sectorLadder:sectorLadder,basketSeries:basketSeries,ret:ret,retSeries:retSeries,rvSeries:rvSeries,pctRankSeries:pctRankSeries,pctVoteSeries:pctVoteSeries,corrPairSeries:corrPairSeries,corrVoteSeries:corrVoteSeries,avgCorrSeries:avgCorrSeries,legFirstActive:legFirstActive,firstNonNull:firstNonNull};
});
