import importlib.util
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
import unittest
spec=importlib.util.spec_from_file_location('client',Path(__file__).with_name('autologin.py'))
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
class Tests(unittest.TestCase):
 def setUp(self):
  p=patch.object(c,"report");p.start();self.addCleanup(p.stop)
 def test_encoding_utf16(self):
  self.assertEqual(c.encode('abc',0),'616263')
  self.assertEqual(c.encode('😀',0),'d83dde00')
 def test_jsonp(self):
  self.assertEqual(c.jsonp('tyutCallback({"result":1});'),{'result':1})
  with self.assertRaises(c.SafeError):c.jsonp('<html>bad</html>')
 def test_online_never_reads_credentials(self):
  with tempfile.TemporaryDirectory() as d,patch.object(c,'BASE',Path(d)),patch.object(c,'CREDENTIALS',Path(d)/'credentials.json'),patch.object(c,'status',return_value={'result':1}),patch.object(c,'read_credentials') as read:
   c.write_json(Path(d)/'retry.json',{'failures':3,'last_attempt':time.time()})
   self.assertEqual(c.once(),0);read.assert_not_called()
   self.assertEqual(c.json.loads((Path(d)/'retry.json').read_text()),{})
 def test_unknown_never_logs_in(self):
  with patch.object(c,'status',return_value={}),patch.object(c,'api') as api:
   with self.assertRaises(c.SafeError):c.once()
   api.assert_not_called()
 def test_private_file(self):
  with tempfile.TemporaryDirectory() as d,patch.object(c,'BASE',Path(d)),patch.object(c,'CREDENTIALS',Path(d)/'credentials.json'):
   c.write_json(c.CREDENTIALS,{'username':'dummy','password':'dummy'})
   self.assertEqual(c.CREDENTIALS.stat().st_mode&0o777,0o600)
   self.assertEqual(c.read_credentials()['username'],'dummy')
   c.CREDENTIALS.chmod(0o644)
   with self.assertRaises(c.SafeError):c.read_credentials()
 def test_attempt_limit(self):
  with tempfile.TemporaryDirectory() as d,patch.object(c,'BASE',Path(d)),patch.object(c,'CREDENTIALS',Path(d)/'credentials.json'),patch.object(c,'status',return_value={'result':0}),patch.object(c,'read_credentials') as read,patch.object(c,'report'):
   c.CREDENTIALS.touch();c.write_json(Path(d)/'retry.json',{'failures':3,'last_attempt':time.time()})
   self.assertEqual(c.once(),2);read.assert_not_called()
if __name__=='__main__':unittest.main()
