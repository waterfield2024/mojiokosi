import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import user_data as ud


def write(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False),'utf-8')


def fixture(root):
    audio=root/'speaker_sources/short.wav';audio.parent.mkdir(parents=True)
    with wave.open(str(audio),'wb') as f:
        f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(b'\x00\x10'*16000)
    write(root/'speaker_memory.json',{'version':1,'profiles':{'voice_1':{'name':'田中','pending_only':True,
          'clips':{'clip1':{'source':str(audio),'origin':str(root/'transcripts/one/result.json'),'start':0.,'end':1.}}}}})
    write(root/'recording_settings.json',{'version':6,'mode':'audio','targets':{},'audio_device':{'name':'old speakers'},
          'processing':{'max_utterance_seconds':30,'pause_seconds':1.5}})
    write(root/'corrections.json',{'version':1,'rules':[],'note':str(audio)})
    write(root/'transcripts/one/result.json',{'source':str(audio),'audio_file':str(audio),'video_source':str(audio),
          'segments':[{'start':0,'end':1,'text':'保存した文章'}]})
    return audio


class PortableData(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve();self.app=self.base/'app';self.app.mkdir()
        self.root=self.app/'UserData';self.old=self.base/'shared';self.old.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(ud,'APP_ROOT',self.app).start()
        patch.object(ud,'data_root',return_value=self.root).start()
        patch.object(ud,'shared_data_root',return_value=self.old).start()
        patch.dict(os.environ,{'WHISPER_LEGACY_DATA_DIR':str(self.old)}).start()
        ud._ready.clear();self.addCleanup(ud._ready.clear)

    def test_first_import_preserves_original_settings_and_short_learning_audio(self):
        audio=fixture(self.old)
        before={str(p.relative_to(self.old)):ud._digest(p) for p in ud._files(self.old)}
        ud.initialize()
        memory=json.loads((self.root/'speaker_memory.json').read_text('utf-8'))
        clip=memory['profiles']['voice_1']['clips']['clip1']
        self.assertEqual(clip['source'],str(self.root/'speaker_sources/short.wav'))
        self.assertEqual((self.root/'speaker_sources/short.wav').read_bytes(),audio.read_bytes())
        for name in ('corrections.json','recording_settings.json'):
            self.assertEqual((self.root/name).read_bytes(),(self.old/name).read_bytes())
        self.assertEqual(before,{str(p.relative_to(self.old)):ud._digest(p) for p in ud._files(self.old)})
        # A folder already carrying data must never merge a different PC's store.
        write(self.old/'transcripts/other/result.json',{'text':'別PCのデータ'})
        ud._ready.clear();ud.initialize()
        self.assertFalse((self.root/'transcripts/other').exists())

    def test_copy_rebases_media_even_while_original_exists_and_keeps_dictionary_text(self):
        fixture(self.old);ud.initialize();original=self.root
        copied=self.base/'copied/UserData';shutil.copytree(original,copied)
        with patch.object(ud,'data_root',return_value=copied):ud.initialize()
        data=json.loads((copied/'transcripts/one/result.json').read_text('utf-8'))
        for key in ('source','audio_file','video_source'):
            self.assertEqual(data[key],str(copied/'speaker_sources/short.wav'))
        self.assertEqual((copied/'corrections.json').read_bytes(),(original/'corrections.json').read_bytes())
        self.assertEqual(json.loads((original/ud.MARKER).read_text('utf-8'))['storage_root'],str(original))

    def test_new_pc_resets_only_device_and_window_identity(self):
        fixture(self.old);ud.initialize()
        saved=json.loads((self.root/'recording_settings.json').read_text('utf-8'))
        saved['targets']={'window':{'hwnd':999,'title':'old'}};write(self.root/'recording_settings.json',saved)
        ud._ready.clear()
        with patch.object(ud,'storage_device',return_value='another-pc'):ud.initialize()
        restored=json.loads((self.root/'recording_settings.json').read_text('utf-8'))
        self.assertEqual(restored['targets'],{});self.assertIsNone(restored['audio_device'])
        self.assertEqual(restored['processing'],saved['processing'])
        self.assertEqual(json.loads((self.root/'speaker_memory.json').read_text('utf-8'))['profiles']['voice_1']['name'],'田中')

    def test_interrupted_relocation_resumes_without_losing_data(self):
        fixture(self.old);ud.initialize();copied=self.base/'copy/UserData';shutil.copytree(self.root,copied)
        original_write=ud._write_json
        def fail_marker(path,value):
            if path.name==ud.MARKER:raise OSError('interrupted')
            original_write(path,value)
        with patch.object(ud,'data_root',return_value=copied):
            with patch.object(ud,'_write_json',side_effect=fail_marker),self.assertRaises(OSError):ud.initialize()
            self.assertEqual(json.loads((copied/ud.MARKER).read_text('utf-8'))['storage_root'],str(self.root))
            ud.initialize()
        self.assertEqual(json.loads((copied/ud.MARKER).read_text('utf-8'))['storage_root'],str(copied))
        self.assertTrue((copied/'speaker_sources/short.wav').is_file())

    def test_invalid_legacy_data_does_not_replace_original(self):
        fixture(self.old);path=self.old/'speaker_memory.json';path.write_text('{','utf-8')
        with self.assertRaises(ValueError):ud.initialize()
        self.assertFalse(self.root.exists());self.assertEqual(path.read_text('utf-8'),'{')

    def test_pending_short_voice_outside_old_data_is_retained(self):
        audio=fixture(self.old);outside=self.base/'original.wav';shutil.copy2(audio,outside)
        memory=json.loads((self.old/'speaker_memory.json').read_text('utf-8'))
        memory['profiles']['voice_1']['clips']['clip1']['source']=str(outside)
        write(self.old/'speaker_memory.json',memory);ud.initialize()
        restored=json.loads((self.root/'speaker_memory.json').read_text('utf-8'))
        source=Path(restored['profiles']['voice_1']['clips']['clip1']['source'])
        self.assertTrue(source.is_relative_to(self.root/'speaker_sources'))
        self.assertEqual(source.read_bytes(),outside.read_bytes())

    def test_internal_missing_media_never_falls_back_to_original_pc(self):
        old=self.base/'old';new=self.base/'new'
        value={'audio_file':str(old/'missing.wav'),'text':str(old/'missing.wav')}
        rebased=ud._relocate(value,old,new,new,require_exists=False)
        self.assertEqual(rebased['audio_file'],str(new/'missing.wav'))
        self.assertEqual(rebased['text'],value['text'])
        escape={'audio_file':str(old/'..'/'outside.wav')}
        self.assertEqual(ud._relocate(escape,old,new,new,require_exists=False),escape)

    @unittest.skipUnless(os.name=='nt','Windows short path aliases')
    def test_short_path_alias_is_imported_and_relocated(self):
        import ctypes
        old=self.base/'shared long folder 日本語';audio=fixture(old)
        kernel=ctypes.WinDLL('kernel32');short=kernel.GetShortPathNameW
        short.argtypes=[ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_uint];short.restype=ctypes.c_uint
        buffer=ctypes.create_unicode_buffer(32768)
        self.assertTrue(short(str(audio),buffer,len(buffer)))
        alias=Path(buffer.value)
        if alias==audio:self.skipTest('Short names are disabled on this volume')
        target=self.root/'speaker_sources/short.wav';target.parent.mkdir(parents=True);shutil.copy2(audio,target)
        value={'source':str(alias),'video_source':str(alias)}
        self.assertEqual(ud._relocate(value,old,self.root,self.root),{'source':str(target),'video_source':str(target)})
        memory={'clips':{'one':{'source':str(alias)}}}
        ud._retain_pending_audio(memory,old,self.root,self.root)
        self.assertEqual(memory['clips']['one']['source'],str(target))

    def test_restore_undo_still_works_after_copy(self):
        from data_backup import create_backup,restore_backup
        from restore_undo import undo_info,undo_restore
        fixture(self.old);ud.initialize()
        archive=self.base/'backup.zip';info=create_backup(archive,include_results=True,root=self.root)
        memory=json.loads((self.root/'speaker_memory.json').read_text('utf-8'))
        memory['profiles']['voice_1']['name']='復元前の名前';write(self.root/'speaker_memory.json',memory)
        restore_backup(archive,expected_fingerprint=info['fingerprint'],root=self.root)
        copied=self.base/'second';shutil.copytree(self.app,copied);new=copied/'UserData'
        with patch.object(ud,'data_root',return_value=new):
            ud.initialize();state=undo_info(new)
            self.assertTrue(state['available'])
            undo_restore(expected_id=state['id'],root=new)
        restored=json.loads((new/'speaker_memory.json').read_text('utf-8'))['profiles']['voice_1']
        self.assertEqual(restored['name'],'復元前の名前')
        self.assertEqual(restored['clips']['clip1']['source'],str(new/'speaker_sources/short.wav'))
        self.assertTrue(Path(restored['clips']['clip1']['source']).is_file())


class PortableRuntime(unittest.TestCase):
    def test_moved_venv_repaired_with_copied_base_preserving_packages(self):
        from portable_runtime import repair_environment
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();base=root/'.python/runtime/python.exe';base.parent.mkdir(parents=True);base.write_bytes(b'fixture')
            cfg=root/'.venv/pyvenv.cfg';cfg.parent.mkdir();cfg.write_text('home = C:/old/runtime','utf-8')
            library=root/'.venv/Lib/site-packages/keep.py';library.parent.mkdir(parents=True);library.write_bytes(b'keep')
            with patch('portable_runtime.subprocess.run') as run:
                self.assertTrue(repair_environment(root))
                self.assertEqual(run.call_args.args[0][0],str(base))
                self.assertIn('--without-pip',run.call_args.args[0])
            self.assertEqual(library.read_bytes(),b'keep')
            cfg.write_text('home = '+str(base.parent),'utf-8')
            python=root/'.venv/Scripts/python.exe';python.parent.mkdir();python.write_bytes(b'fixture')
            with patch('portable_runtime.subprocess.run') as run:
                self.assertFalse(repair_environment(root));run.assert_not_called()


if __name__=='__main__':unittest.main()
