import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlmodel import Session, select, desc
from googleapiclient.discovery import build
from db.user_emails import UserEmails
from db import processing_tasks as task_models
from db.utils.user_email_utils import create_user_email
from utils.auth_utils import AuthenticatedUser
from utils.email_utils import get_email_ids, get_email
from utils.llm_utils import process_email
from utils.config_utils import get_settings
from session.session_layer import validate_session
import database
from google.oauth2.credentials import Credentials
import json
from start_date.storage import get_start_date_email_filter
from constants import QUERY_APPLIED_EMAIL_FILTER
from datetime import datetime, timedelta
from slowapi import Limiter
from slowapi.util import get_remote_address
from tasks.email_tasks import fetch_and_process_emails as celery_fetch_emails
from celery.result import AsyncResult
from celery_app import celery_app

limiter = Limiter(key_func=get_remote_address)

# Logger setup
logger = logging.getLogger(__name__)

# Get settings
settings = get_settings()
APP_URL = settings.APP_URL

SECONDS_BETWEEN_FETCHING_EMAILS = 1 * 60 * 60  # 1 hour

# FastAPI router for email routes
router = APIRouter()

@router.get("/processing", response_class=HTMLResponse)
async def processing(request: Request, db_session: database.DBSession, user_id: str = Depends(validate_session)):
    logging.info("user_id:%s processing", user_id)
    if not user_id:
        logger.info("user_id: not found, redirecting to login")
        return RedirectResponse("/logout", status_code=303)

    process_task_run: task_models.TaskRuns = db_session.get(task_models.TaskRuns, user_id)

    if process_task_run is None:
        raise HTTPException(
            status_code=404, detail="Processing has not started."
        )

    # Get current values before checking Celery status
    current_status = process_task_run.status
    current_processed = process_task_run.processed_emails
    current_total = process_task_run.total_emails
    celery_task_id = process_task_run.celery_task_id

    # Check Celery task status if task ID is available
    if celery_task_id:
        try:
            result = AsyncResult(celery_task_id, app=celery_app)
            if result.state == 'PROGRESS':
                meta = result.info
                logger.info(f"Celery task in progress: {meta}")
                # Update progress from Celery task
                if meta and 'current' in meta:
                    current_processed = meta['current']
        except Exception as e:
            logger.error(f"Error checking Celery task status: {e}")

    if current_status == task_models.FINISHED:
        logger.info("user_id: %s processing complete", user_id)
        return JSONResponse(
            content={
                "message": "Processing complete",
                "processed_emails": current_processed,
                "total_emails": current_total,
            }
        )
    else:
        logger.info("user_id: %s processing not complete for file", user_id)
        return JSONResponse(
            content={
                "message": "Processing in progress",
                "processed_emails": current_processed,
                "total_emails": current_total,
            }
        )


@router.get("/get-emails", response_model=List[UserEmails])
@limiter.limit("5/minute")
def query_emails(request: Request, db_session: database.DBSession, user_id: str = Depends(validate_session)) -> None:
    try:
        logger.info(f"Fetching emails for user_id: {user_id}")

        # Query emails sorted by date (newest first)
        statement = select(UserEmails).where(UserEmails.user_id == user_id).order_by(desc(UserEmails.received_at))
        user_emails = db_session.exec(statement).all()

        # Filter out records with "unknown" application status
        filtered_emails = [
            email for email in user_emails 
            if email.application_status and email.application_status.lower() != "unknown"
        ]

        logger.info(f"Found {len(user_emails)} total emails, returning {len(filtered_emails)} after filtering out 'unknown' status")
        return filtered_emails  # Return filtered list

    except Exception as e:
        logger.error(f"Error fetching emails for user_id {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")
        

@router.delete("/delete-email/{email_id}")
async def delete_email(request: Request, db_session: database.DBSession, email_id: str, user_id: str = Depends(validate_session)):
    """
    Delete an email record by its ID for the authenticated user.
    """
    try:
        # Query the email record to ensure it exists and belongs to the user
        email_record = db_session.exec(
            select(UserEmails).where(
                (UserEmails.id == email_id) & (UserEmails.user_id == user_id)
            )
        ).first()

        if not email_record:
            logger.warning(f"Email with id {email_id} not found for user_id {user_id}")
            raise HTTPException(
                status_code=404, detail=f"Email with id {email_id} not found"
            )

        # Delete the email record
        db_session.delete(email_record)
        db_session.flush()

        logger.info(f"Email with id {email_id} deleted successfully for user_id {user_id}")
        return {"message": "Item deleted successfully"}

    except Exception as e:
        logger.error(f"Error deleting email with id {email_id} for user_id {user_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to delete email: {str(e)}"
        )
        

@router.post("/fetch-emails")
@limiter.limit("5/minute")
async def start_fetch_emails(
    request: Request, db_session: database.DBSession, user_id: str = Depends(validate_session)
):
    """Starts the Celery task for fetching and processing emails."""
    
    if not user_id:
        raise HTTPException(status_code=403, detail="Unauthorized")
    logger.info(f"user_id:{user_id} start_fetch_emails")
    
    # Check if user already has an active task
    process_task_run = db_session.get(task_models.TaskRuns, user_id)
    if process_task_run and process_task_run.status == task_models.STARTED:
        # Check if Celery task is still running
        if process_task_run.celery_task_id:
            result = AsyncResult(process_task_run.celery_task_id, app=celery_app)
            if result.state in ['PENDING', 'STARTED', 'PROGRESS']:
                logger.info(f"Task already running for user {user_id}")
                return JSONResponse(
                    content={"message": "Email fetching already in progress"}, 
                    status_code=202
                )
    
    # Retrieve stored credentials
    creds_json = request.session.get("creds")
    if not creds_json:
        logger.error(f"Missing credentials for user_id: {user_id}")
        return HTMLResponse(content="User not authenticated. Please log in again.", status_code=401)

    try:
        # Convert JSON string back to dict for Celery
        creds_dict = json.loads(creds_json)
        
        # Get additional parameters from session
        start_date = request.session.get("start_date")
        is_new_user = request.session.get("is_new_user", False)
        
        # Get last updated email date
        last_email = db_session.exec(
            select(UserEmails)
            .where(UserEmails.user_id == user_id)
            .order_by(desc(UserEmails.received_at))
        ).first()
        
        last_updated = last_email.received_at.isoformat() if last_email else None

        logger.info(f"Starting Celery email task for user_id: {user_id}")

        # Start Celery task
        task = celery_fetch_emails.delay(
            user_id=user_id,
            creds_dict=creds_dict,
            start_date=start_date,
            is_new_user=is_new_user,
            last_updated=last_updated
        )
        
        # Update session to mark user as not new
        request.session["is_new_user"] = False

        return JSONResponse(
            content={
                "message": "Email fetching started",
                "task_id": task.id
            }, 
            status_code=200
        )
    except Exception as e:
        logger.error(f"Error starting email task: {e}")
        raise HTTPException(status_code=500, detail="Failed to start email processing")


# Note: The old fetch_emails_to_db function has been replaced by Celery task in tasks/email_tasks.py