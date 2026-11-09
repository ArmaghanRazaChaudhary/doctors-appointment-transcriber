"""Web usage ledger: SQLite is authoritative; JSON export failure cannot lose API output."""
import json
import os
from time import perf_counter
import store
from clinical_app.accounting import CallLog as OriginalCallLog

class CallLog(OriginalCallLog):
    def __init__(self,run_dir):
        super().__init__(run_dir)
        self.snapshot={};self.persistence_warning=None
    def save(self):
        known=[c['estimated_cost']['amount'] for c in self.calls if c.get('estimated_cost') and c['estimated_cost']['amount'] is not None]
        complete=bool(self.calls) and len(known)==len(self.calls)
        summary={'calls':self.calls,'elapsed_seconds':round(perf_counter()-self.started,3),'estimated_cost_usd':round(sum(known),9) if complete else None,'known_estimated_subtotal_usd':round(sum(known),9),'estimate_complete':complete,'reported_cost_usd':None,'reconciliation_status':'not_reconciled'}
        self.snapshot=summary
        try:
            with store.connection() as db:
                db.execute('CREATE TABLE IF NOT EXISTS api_logs (path TEXT PRIMARY KEY, document TEXT NOT NULL)')
                db.execute('INSERT INTO api_logs VALUES (?,?) ON CONFLICT(path) DO UPDATE SET document=excluded.document',(str(self.path),json.dumps(summary,ensure_ascii=False)))
        except Exception as exc:
            # Before an API request, failure to record its start must stop the request.
            if self.calls and self.calls[-1]['status']=='started':raise
            self.persistence_warning=type(exc).__name__
        try:
            with self.path.open('w',encoding='utf-8') as f:
                json.dump(summary,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
        except OSError as exc:
            self.persistence_warning=type(exc).__name__
        if self.persistence_warning:summary['accounting_warning']='Usage export needs attention: '+self.persistence_warning
        return summary
    def document(self):
        return json.loads(json.dumps(self.snapshot,ensure_ascii=False))
