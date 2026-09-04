-- Provider runtime selections discovered dynamically from account runtimes.
-- Null means the provider should use its account/default selection.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS provider_model TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS provider_mode TEXT;
