"""Original WinRT processing initialization with process-local COM test inputs.

Every DLL state transition is through its original function. Test doubles only
implement endpoint/collection/property-store inputs; no DSP or connection flags
are written. The endpoint is fictional and no endpoint is registered.
"""
import argparse,ctypes as C,hashlib,json,pathlib,sys,uuid,struct,threading,time,wave,math,array
HERE=pathlib.Path(__file__).resolve().parent;ROOT=HERE.parents[2]
sys.path.insert(0,str(HERE));sys.path.insert(0,str(HERE.parent))
from local_property_store import LocalPropertyStore,key_bytes,key_text,E_NOINTERFACE,E_NOTIMPL
CALL=C.WINFUNCTYPE;HR=C.c_int32;PTR=C.c_void_p;U32=C.c_uint32
PIN='0fada40251880d03c657a357745d362799c93fb6024f733f1c8bf1057f5e4715'
def sha(b):return hashlib.sha256(b).hexdigest()
def guid(s):return C.create_string_buffer(uuid.UUID(s).bytes_le,16)
def emit(stage,**x):print(json.dumps({'stage':stage,**x}),flush=True,file=sys.stderr)
def meth(p,n,result,*args):
 return CALL(result,PTR,*args)(PTR.from_address(PTR.from_address(p).value+n*8).value)
def getptr(p):return PTR.from_address(p).value or 0
class Store(LocalPropertyStore):
 def _get(self,this,key,variant):
  raw=C.string_at(key,20);spec=self.values.get(raw)
  emit('property-query',key=key_text(raw),supplied=bool(spec))
  if not spec or spec['kind'] not in ('lpwstr','blob_bytes'):return super()._get(this,key,variant)
  C.memset(variant,0,24);rec={'key':key_text(raw),'name':spec['name'],'kind':spec['kind'],'supplied':True};self.audit.append(rec)
  b=spec['value'].encode('utf-16le')+b'\0\0' if spec['kind']=='lpwstr' else spec['value']
  p=self.ole32.CoTaskMemAlloc(len(b));C.memmove(p,b,len(b))
  C.c_uint16.from_address(variant).value=31 if spec['kind']=='lpwstr' else 65
  if spec['kind']=='lpwstr':PTR.from_address(variant+8).value=p
  else:U32.from_address(variant+8).value=len(b);PTR.from_address(variant+16).value=p;rec.update(bytes=len(b),sha256=sha(b))
  return 0
class FakeCom:
 def __init__(self,name,methods,known,audit):
  self.name=name;self.refs=1;self.known={uuid.UUID(s).bytes_le for s in known};self.audit=audit
  self.known.add(uuid.UUID('00000000-0000-0000-c000-000000000046').bytes_le)
  def qi(this,iid,out):
   raw=C.string_at(iid,16);ok=raw in self.known;PTR.from_address(out).value=self.pointer if ok else None
   self.audit.append({'object':self.name,'method':'QueryInterface','iid':str(uuid.UUID(bytes_le=raw)),'supported':ok});return 0 if ok else E_NOINTERFACE
  def add(_):self.refs+=1;return self.refs
  def release(_):self.refs=max(1,self.refs-1);return self.refs
  self.cb=[CALL(HR,PTR,PTR,PTR)(qi),CALL(U32,PTR)(add),CALL(U32,PTR)(release)]
  self.cb +=[CALL(ret,PTR,*args)(fn) for ret,args,fn in methods]
  self.vt=(PTR*len(self.cb))(*(C.cast(f,PTR).value for f in self.cb));self.obj=(PTR*1)(C.addressof(self.vt));self.pointer=C.addressof(self.obj)
class Guarded:
 def __init__(self,size):
  self.size=size;self.owner=C.create_string_buffer(size+256);self.pointer=(C.addressof(self.owner)+127)&~63
  self.canary=bytes([0xa5])*64
  C.memmove(self.pointer-64,self.canary,64);C.memmove(self.pointer+size,self.canary,64)
 def check(self):return C.string_at(self.pointer-64,64)==self.canary and C.string_at(self.pointer+self.size,64)==self.canary
class HrtfFrontendAdapter:
 """Pinned original frontend. Each call accepts mono planes for static objects.

 Owns process-local COM input doubles; invokes original constructor, Init,
 SetAudioObject, MetadataGenerate and IDAPModule2 EncodeAudioData path. No live
 endpoint registration and no writes to original private state.
 """
 def __init__(self,access_dll,hrtf_dll,init_blob,initial_runtime_blob,mode=0,static_mask=0x3fffe):
  from types import SimpleNamespace
  a=SimpleNamespace(dll=pathlib.Path(access_dll),hrtf=pathlib.Path(hrtf_dll),init_blob=pathlib.Path(init_blob),runtime_blob=pathlib.Path(initial_runtime_blob),initial_runtime_blob=pathlib.Path(initial_runtime_blob),mode=mode,static_mask=static_mask,object_type=None)
  assert sha(a.dll.read_bytes())==PIN
  assert sha(a.hrtf.read_bytes())=='3da34330b36a79d6c4e42220190ee8239cad65564911c248ebda59d237bf9f88'
  init=a.init_blob.read_bytes();runtime=a.runtime_blob.read_bytes();initialruntime=a.initial_runtime_blob.read_bytes();audit=[];references=[]
  C.windll.kernel32.SetErrorMode(0x8003)
  def exception_handler(info):
   try:
    record=getptr(info);code=U32.from_address(record).value;address=getptr(record+16)
    if code!=0x406d1388:emit('original-process-exception',code=hex(code),address=hex(address),rva=hex(address-dll._handle) if 'dll' in locals() else None)
   except Exception:pass
   return 0
  exception_callback=CALL(C.c_long,PTR)(exception_handler)
  C.windll.kernel32.AddVectoredExceptionHandler.argtypes=[U32,PTR];C.windll.kernel32.AddVectoredExceptionHandler.restype=PTR
  C.windll.kernel32.AddVectoredExceptionHandler(1,C.cast(exception_callback,PTR))
  combase=C.WinDLL('combase');combase.RoInitialize.argtypes=[U32];combase.RoInitialize.restype=HR;combase.RoInitialize(1)
  combase.WindowsCreateString.argtypes=[C.c_wchar_p,U32,PTR];combase.WindowsCreateString.restype=HR
  combase.WindowsDeleteString.argtypes=[PTR];combase.WindowsDeleteString.restype=HR
  dll=C.WinDLL(str(a.dll.resolve()));factory=PTR();hs=PTR();instance=PTR();name='Dolby.DolbyAudioProcessingModule'
  combase.WindowsCreateString(name,len(name),C.byref(hs));dll.DllGetActivationFactory.argtypes=[PTR,PTR];dll.DllGetActivationFactory.restype=HR
  hr=dll.DllGetActivationFactory(hs,C.byref(factory));assert hr==0
  hr=meth(factory.value,6,HR,PTR)(factory,C.byref(instance));assert hr==0;emit('factory-created')
  def query(iid):
   ptr=PTR();hr=meth(instance.value,0,HR,PTR,PTR)(instance,guid(iid),C.byref(ptr));assert hr==0;references.append(ptr);return ptr.value
  processing=query('2b2e53bc-651b-4a90-8f64-38531c154fda');context=query('67b16434-1ef3-47bc-8d2b-f485ee148c32')
  testguid=str(uuid.uuid4());endpointid='Dolby-Offline-ASAR-Test-'+testguid
  props=Store({key_bytes('1da5d803-d492-4edd-8c23-e0c0ffee7f0e',4):{'kind':'lpwstr','name':'PKEY_AudioEndpoint_GUID','value':'{'+testguid+'}'},
               key_bytes('a45c254e-df1c-4efd-8020-67d146a850e0',14):{'kind':'lpwstr','name':'PKEY_Device_InstanceId','value':'SWD\\MMDEVAPI\\'+testguid}})
  values={key_bytes('1b4dab55-b1fb-4d8c-8317-f2d4a96efbb8',pid):{'kind':'blob_bytes','name':('init'if pid in(1,4)else'runtime')+str(pid),'value':init if pid in(1,4)else initialruntime} for pid in(1,2,4,5)}
  user=Store(values)
  def opened(this,mode,out):
   PTR.from_address(out).value=user.pointer;audit.append({'object':'FxStore','method':'OpenPropertyStore','mode':mode});return 0
  fx=FakeCom('FxStore',[(HR,[U32,PTR],opened)]*3+[(HR,[PTR],lambda *_:E_NOTIMPL)]*2,['302ae7f9-d7e0-43e4-971b-1f8293613d2a'],audit)
  def activate(this,iid,flags,pv,out):
   raw=C.string_at(iid,16);rec={'object':'Device','method':'Activate','iid':str(uuid.UUID(bytes_le=raw)),'flags':flags}
   if pv:rec['variant_type']=C.c_uint16.from_address(pv).value;rec['context']=str(uuid.UUID(bytes_le=C.string_at(getptr(pv+8),16)))
   audit.append(rec);PTR.from_address(out).value=fx.pointer;return 0
  ole=C.WinDLL('ole32');ole.CoTaskMemAlloc.argtypes=[C.c_size_t];ole.CoTaskMemAlloc.restype=PTR
  def devid(this,out):
   b=(endpointid+'\0').encode('utf-16le');p=ole.CoTaskMemAlloc(len(b));C.memmove(p,b,len(b));PTR.from_address(out).value=p;return 0
  def openprops(this,mode,out):PTR.from_address(out).value=props.pointer;return 0
  def state(this,out):U32.from_address(out).value=1;return 0
  device=FakeCom('Device',[(HR,[PTR,U32,PTR,PTR],activate),(HR,[U32,PTR],openprops),(HR,[PTR],devid),(HR,[PTR],state)],['d666063f-1587-4e43-81f1-b948e807363f'],audit)
  def count(this,out):U32.from_address(out).value=1;audit.append({'object':'Collection','method':'GetCount'});return 0
  def item(this,index,out):PTR.from_address(out).value=device.pointer;audit.append({'object':'Collection','method':'Item','index':index});return 0
  coll=FakeCom('Collection',[(HR,[PTR],count),(HR,[U32,PTR],item)],['0bd7a1be-7a1a-44db-8397-cc5392387b5e'],audit)
  # APOInitSystemEffects3 from the pinned Microsoft SDK, naturally aligned x64.
  initdata=C.create_string_buffer(0x50);U32.from_buffer(initdata).value=0x50
  PTR.from_buffer(initdata,0x18).value=props.pointer;PTR.from_buffer(initdata,0x28).value=coll.pointer
  mode=uuid.UUID('c18e2f7e-933d-4965-b7d1-1eef228d2af3').bytes_le;C.memmove(C.addressof(initdata)+0x38,mode,16)
  # Normal original constructor creates the engine and original TraceLogger.
  hrtf=C.WinDLL(str(a.hrtf.resolve()))
  enginebuf=Guarded(0x2c9d8);engine=enginebuf.pointer
  pluginbuf=Guarded(0x388);plugin=pluginbuf.pointer
  ctor=CALL(PTR,PTR)(hrtf._handle+0x5434);pluginctor=CALL(PTR,PTR)(hrtf._handle+0x3754)
  pluginctor(plugin);ctor(engine);logger=plugin+0xe8
  personality=guid('1459ac38-3875-49bf-bb59-0fe80f4d395d' if a.mode==0 else '4c81e564-c8ef-4ad9-9f2c-9ef995533790')
  initialize=CALL(HR,PTR,PTR,PTR,PTR,PTR,U32,U32,U32,U32,U32,PTR,PTR,PTR,PTR)(hrtf._handle+0x5c40)
  emit('before-original-Hrtf-RuntimeClassInitialize',object_type=None,logger_offset='0xe8')
  hr=initialize(engine,personality,None,processing,None,256,48000,a.static_mask,2,3,None,logger,None,initdata)
  emit('after-original-Hrtf-RuntimeClassInitialize',hresult=hex(hr&0xffffffff))
  assert hr==0
  self.keep=[references,exception_callback,combase,dll,hrtf,enginebuf,pluginbuf,props,user,fx,device,coll,initdata,personality,factory,instance]
  self.processing=processing;self.engine=engine;self.module=getptr(processing+0x538);self.dsp=getptr(self.module+0x60)
  self.dll=dll;self.hrtf=hrtf;self.mode=a.mode;self.user=user;self.device=device;self.audit=audit
  self.engine_guard=enginebuf;self.plugin_guard=pluginbuf
  self.setobject=CALL(HR,PTR,PTR,PTR,U32,C.c_float)(hrtf._handle+0x7b6c)
  self.process_original=CALL(HR,PTR,U32,PTR,PTR)(hrtf._handle+0x6afc)
  self.mono=Guarded(256*4);self.stereo=Guarded(512*4);self.spec=Guarded(32)
  self.mono_values=(C.c_float*256).from_address(self.mono.pointer)
  self.out_values=(C.c_float*512).from_address(self.stereo.pointer)
  self.frame_index=0;self.timings=[];self.metadata_snapshots=[]
  self.configuration=list((U32*8).from_address(engine+0x48))
  self.initialization={'hresult':hex(hr&0xffffffff),'configuration_u32':self.configuration,'core_initialization_readback':{name:U32.from_address(self.dsp+off).value for name,off in [('sample_rate',0),('mi_process_disable',4),('virtual_bass_process_enable',8),('mode',12),('max_num_objects',16),('max_num_bypass_objects',20)]},'com_audit':audit,'initial_user_property_queries':list(user.audit)}
 def notify(self,blob):
  out=PTR();hr=meth(self.processing,0,HR,PTR,PTR)(self.processing,guid('56b0c76f-02fd-4b21-a52e-9f8219fc86e4'),C.byref(out));assert hr==0
  key=key_bytes('1b4dab55-b1fb-4d8c-8317-f2d4a96efbb8',2 if self.mode==0 else 5)
  self.user.values[key]['value']=blob;before=len(self.user.audit)
  note=C.create_string_buffer(72);U32.from_buffer(note).value=3;PTR.from_buffer(note,8).value=self.device.pointer
  C.memmove(C.addressof(note)+16,uuid.UUID('45da5c30-2837-4ac4-b1e2-50acc3865974').bytes_le,16)
  U32.from_buffer(note,32).value=1;PTR.from_buffer(note,40).value=self.user.pointer;C.memmove(C.addressof(note)+48,key,20)
  pre=C.string_at(self.dsp,0x3900);meth(out.value,4,None,PTR)(out,note);post=C.string_at(self.dsp,0x3900)
  meth(out.value,2,U32)(out)
  queries=self.user.audit[before:];assert any(x['key']==key_text(key)for x in queries)
  return {'blob_sha256':sha(blob),'callback_queries':queries,'same_module':getptr(self.processing+0x538)==self.module,'same_dsp':getptr(self.module+0x60)==self.dsp,'changed_prefix_byte_offsets':[i for i,(x,y)in enumerate(zip(pre,post))if x!=y]}
 def process(self,objects):
  # objects = [(AudioObjectType single static bit, iterable256float), ...]
  start=time.perf_counter();packet_indices=[]
  for typ,values in objects:
   assert typ>=2 and typ<=0x20000 and typ&(typ-1)==0
   data=array.array('f',values);assert len(data)==256
   C.memmove(self.mono.pointer,data.tobytes(),1024)
   spec=(U32*8).from_address(self.spec.pointer)
   spec[:]=[1,0,typ,3 if self.frame_index==0 else 2,0,0,0,0x3f800000]
   hr=self.setobject(self.engine,self.spec.pointer,self.mono.pointer,256,1.0);assert hr==0
   assert self.mono.check() and self.spec.check() and C.string_at(self.mono.pointer,1024)==data.tobytes()
   idx=typ.bit_length()-2;packet_indices.append(idx)
  written=U32();hr=self.process_original(self.engine,2048,self.stereo.pointer,C.byref(written))
  assert hr==0 and written.value==2048
  self.timings.append(time.perf_counter()-start)
  assert self.stereo.check() and self.engine_guard.check() and self.plugin_guard.check()
  assert getptr(self.processing+0x538)==self.module and getptr(self.module+0x60)==self.dsp
  if self.frame_index in(0,64):
   self.metadata_snapshots.append({'block':self.frame_index,'objects':[{'index':i,'packet_active':C.c_uint8.from_address(self.engine+0x68+i*0xa0+16).value,'packet_metadata_hex':C.string_at(getptr(self.engine+0x68+i*0xa0+8),48).hex(),'audio_object_type':U32.from_address(self.engine+0x5c68+i*0x2b8+0x2a0).value}for i in packet_indices]})
  self.frame_index+=1
  return C.string_at(self.stereo.pointer,2048)

 def state_readback(self):
  return {'requested_processing_mode':U32.from_address(self.dsp+0x118).value,'applied_processing_mode':U32.from_address(self.dsp+0x11c).value,'requested_matrix_enable':U32.from_address(self.dsp+0x128).value,'applied_matrix_enable':U32.from_address(self.dsp+0x12c).value,'requested_matrix_output_channels':U32.from_address(self.dsp+0x120).value,'applied_matrix_output_channels':U32.from_address(self.dsp+0x124).value,'module_initialized':C.c_uint8.from_address(self.module+0x68).value,'default_headphone_tuning_initialized':C.c_uint8.from_address(self.module+0x69).value,'core_mode':U32.from_address(self.dsp+0xc).value,'sample_rate':U32.from_address(self.dsp).value,'max_num_objects':U32.from_address(self.dsp+0x10).value,'max_num_bypass_objects':U32.from_address(self.dsp+0x14).value}

class PcmFrontendAdapter:
 """Process-scoped adapter for 48k/256f interleaved float32 channel beds.

 Windows WAVE-order 6/8-channel PCM is assigned to original static audio
 objects. Atmos object metadata absent from PCM cannot be reconstructed.
 """
 def __init__(self,dll,hrtf,init_blob,initial_runtime_blob,input_channels=6,mode=0,runtime_blob=None):
  assert input_channels in(1,2,6,8)
  self.layout={1:[2],2:[2,4],6:[2,4,8,16,128,256],8:[2,4,8,16,128,256,32,64]}[input_channels]
  self.engine=HrtfFrontendAdapter(dll,hrtf,init_blob,initial_runtime_blob,mode)
  self.notification=self.engine.notify(pathlib.Path(runtime_blob).read_bytes())if runtime_blob else None
  self.metadata={'block_frames':256,'sample_rate':48000,'input_channels':input_channels,'output_channels':2,'format':'float32le-interleaved','engine':'original-hrtf-frontend','mode':mode,'static_audio_object_layout':self.layout,'full_frontend':True,'runtime_notification':self.notification}
  self.blocks=0
 def process(self,payload):
  assert len(payload)==256*4*len(self.layout)
  samples=array.array('f');samples.frombytes(payload)
  rendered=self.engine.process([(typ,samples[ci::len(self.layout)])for ci,typ in enumerate(self.layout)])
  self.blocks+=1
  return rendered
 def close(self):
  # Original frontend owns background/module lifetime for this process. The
  # caller terminates its dedicated worker after this result; no private frees.
  return {'blocks':self.blocks,'original_processing_seconds':sum(self.engine.timings),'all_host_guards_unchanged':self.engine.engine_guard.check()and self.engine.plugin_guard.check(),'lifetime':'dedicated worker process'}


