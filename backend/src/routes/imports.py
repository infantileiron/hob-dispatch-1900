import logging

from fastapi import APIRouter, Depends, HTTPException

from src.constants.enums import Role
from src.controllers.session import account, allows
from src.models.db import connect
from src.schemas.inputs import ImportInput
from src.services import imports
from src.utils.encoding import payload

router = APIRouter()
writer = allows(Role.MANAGER, Role.RECORDER)


@router.post('/imports/preview')
def preview_import(body: ImportInput, actor=Depends(writer)):
    with connect() as db:
        return imports.preview(db, body)


@router.post('/imports', status_code=201)
def submit_import(body: ImportInput, actor=Depends(writer)):
    with connect() as db:
        batch_id = imports.submit(db, actor, body)
    try:
        imports.run_batch(batch_id, actor['email'])
    except Exception:
        # Anything left unprocessed stays pending; GET/retry/resume can settle it and
        # already settled shifts keep the snapshot they finished with.
        logging.getLogger('imports').exception('Batch %s interrupted', batch_id)
    with connect() as db:
        return imports.batch_view(db, batch_id)


@router.get('/imports', dependencies=[Depends(account)])
def list_imports():
    with connect() as db:
        return imports.recent(db)


@router.get('/imports/{batch_id}', dependencies=[Depends(account)])
def get_import(batch_id: int):
    with connect() as db:
        return imports.batch_view(db, batch_id)


@router.post('/imports/{batch_id}/resume')
def resume_import(batch_id: int, actor=Depends(writer)):
    with connect() as db:
        batch = db.execute('SELECT id FROM import_batches WHERE id=%s FOR UPDATE',
                           (batch_id,)).fetchone()
    if batch is None:
        raise HTTPException(404, '导入批次不存在')
    imports.run_batch(batch_id, actor['email'])
    with connect() as db:
        return imports.batch_view(db, batch_id)


@router.get('/shifts/{shift_id}/imports', dependencies=[Depends(account)])
def shift_imports(shift_id: int):
    with connect() as db:
        return payload(imports.history(db, shift_id))
