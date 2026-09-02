export type Env = {
  DATABASE_URL: string;
  GITHUB_TOKEN: string;
  GITHUB_CALLBACK_SECRET: string;
  HASSAN_BOOTSTRAP_TOKEN?: string;
  HASSAN_ENV: string;
  HASSAN_VERSION: string;
  GITHUB_REPO: string;
  GITHUB_WORKFLOW_FILE: string;
  ARTIFACT_BACKEND: string;
};

export type JobRow = {
  id: string;
  project_id: string;
  conversation_id: string | null;
  goal: string;
  job_type: string;
  state: string;
  result_summary: string | null;
  log: string;
  created_at: number;
  updated_at: number;
  idempotency_key: string | null;
  checkpoint_stage: string;
  cancel_requested: number;
  github_run_id: string | null;
  github_workflow: string | null;
  dispatch_attempt: number;
  last_dispatch_at: number | null;
  failure_reason: string | null;
};
