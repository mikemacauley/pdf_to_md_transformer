// Independent DEE-1 JavaScript encoder. Unicode code points, never UTF-16 offsets.
import { createHash } from 'node:crypto';
const scalarCompare = (a,b) => {
  const x=Array.from(a, c=>c.codePointAt(0)), y=Array.from(b,c=>c.codePointAt(0));
  for(let i=0;i<Math.min(x.length,y.length);i++) if(x[i]!==y[i]) return x[i]-y[i];
  return x.length-y.length;
};
function string(s) {
  for (const c of s) if(c.codePointAt(0)>=0xd800 && c.codePointAt(0)<=0xdfff) throw Error('surrogate');
  return JSON.stringify(s);
}
function encode(v) {
  if(v===null) return 'null';
  if(typeof v==='boolean') return v?'true':'false';
  if(typeof v==='number') {
    if(!Number.isSafeInteger(v)||Object.is(v,-0)) throw Error('unsafe integer');
    return String(v);
  }
  if(typeof v==='string') return string(v);
  if(Array.isArray(v)) return '['+v.map(encode).join(',')+']';
  if(typeof v==='object'&&Object.getPrototypeOf(v)===Object.prototype)
    return '{'+Object.keys(v).sort(scalarCompare).map(k=>string(k)+':'+encode(v[k])).join(',')+'}';
  throw Error('unsupported value');
}
export const canonical = v => Buffer.from(encode(v)+'\n','utf8');
export const digest = bytes => createHash('sha256').update(bytes).digest('hex');
export function decodeCanonical(bytes) {
  const text=new TextDecoder('utf-8',{fatal:true}).decode(bytes), result=JSON.parse(text);
  if(!canonical(result).equals(Buffer.from(bytes))) throw Error('noncanonical input');
  return result;
}
export function sliceCodepoints(text,start,end) {
  const points=Array.from(text);
  if(!Number.isSafeInteger(start)||!Number.isSafeInteger(end)||start<0||end<start||end>points.length) throw Error('invalid range');
  return points.slice(start,end).join('');
}
