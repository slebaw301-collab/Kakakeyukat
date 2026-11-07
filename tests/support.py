"""Offline module loading; synthetic credentials, never call main or real services."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]

def load_bot(variant='candidate'):
    path = ROOT / {'baseline': 'baseline/bot.py', 'audit': 'audit_candidate.py', 'phase1': 'phase1.py', 'phase2':'phase2.py', 'trace':'tail_before_fix.py'}.get(variant, 'bot.py')
    # Deliberately overwrite inherited credentials for every test and benchmark.
    os.environ.update(BOT_TOKEN='123456:OFFLINE_TEST_ONLY', ADMIN_ID='1',
                      DATABASE_URL='postgresql://invalid.invalid/offline_test',
                      AUTOGOPAY_API_KEY='OFFLINE_TEST_ONLY', RUN_DB_MIGRATIONS_ON_STARTUP='0')
    spec = importlib.util.spec_from_file_location('audit_' + variant, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def close_bot(module):
    for name in ('_db_executor', '_db_admin_executor', '_cpu_executor'):
        getattr(module, name).shutdown(wait=True, cancel_futures=True)

def update(data='buy', uid=42, query_id='test-query', message_id=1):
    user = NS(id=uid, full_name='Synthetic buyer')
    msg = NS(message_id=message_id, photo=(), text_html='', reply_markup=None,
             reply_text=AsyncMock(), delete=AsyncMock())
    query = NS(id=query_id, data=data, from_user=user, message=msg,
               answer=AsyncMock(), edit_message_text=AsyncMock(), edit_message_caption=AsyncMock())
    return NS(callback_query=query, message=None, update_id=query_id,
              effective_user=user, effective_chat=NS(id=uid, type='private'), effective_message=msg)
