-- =============================================================================
-- Migration: Add source to liner_eta_history, update log trigger, and fix integrity RPC
-- Supports 'Web' and 'Manual Input' labels for Liner ETA audit logs
-- =============================================================================

-- 1. Add source column to liner_eta_history if not exists
ALTER TABLE public.liner_eta_history 
ADD COLUMN IF NOT EXISTS source text DEFAULT 'Web';

-- 2. Backfill existing records:
-- If changed_by is set, it was a manual input from dashboard.
-- Otherwise it was from automated scraper / system sync.
UPDATE public.liner_eta_history
SET source = CASE 
  WHEN changed_by IS NOT NULL THEN 'Manual Input' 
  ELSE 'Web' 
END
WHERE source IS NULL OR source = '';

-- 3. Trigger Function on vessel_schedules:
-- Automatically records 'Manual Input' if auth.uid() is present, or 'Web' if automated.
CREATE OR REPLACE FUNCTION public.log_liner_eta_change()
RETURNS trigger AS $$
BEGIN
  IF (OLD.liner_eta IS DISTINCT FROM NEW.liner_eta) THEN
    INSERT INTO public.liner_eta_history (
      schedule_id,
      old_eta,
      new_eta,
      changed_at,
      changed_by,
      source
    ) VALUES (
      NEW.id,
      OLD.liner_eta,
      NEW.liner_eta,
      now(),
      auth.uid()::text,
      CASE WHEN auth.uid() IS NOT NULL THEN 'Manual Input' ELSE 'Web' END
    );
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Recreate trigger if needed
DROP TRIGGER IF EXISTS trg_log_liner_eta_change ON public.vessel_schedules;
CREATE TRIGGER trg_log_liner_eta_change
AFTER UPDATE OF liner_eta ON public.vessel_schedules
FOR EACH ROW
EXECUTE FUNCTION public.log_liner_eta_change();

-- 4. Fix get_service_eta_integrity RPC (add missing FROM departed_with_ata)
CREATE OR REPLACE FUNCTION get_service_eta_integrity(p_service TEXT)
RETURNS JSON AS $$
DECLARE
    v_result JSON;
BEGIN
    WITH departed_with_ata AS (
        SELECT
            vs.id,
            mv.vessel_name,
            vs.voyage,
            vs.liner_eta,
            COALESCE(vs.eta_planner, tl_pred.predicted_eta_ml) AS predicted_eta,
            COALESCE(vs.actual_ata, vs.ata) AS ata,
            CASE 
                WHEN vs.liner_eta IS NOT NULL AND COALESCE(vs.actual_ata, vs.ata) IS NOT NULL 
                THEN ABS(EXTRACT(EPOCH FROM (COALESCE(vs.actual_ata, vs.ata)::timestamp - vs.liner_eta::timestamp)) / 3600.0)
                ELSE NULL
            END AS liner_error_hours,
            CASE 
                WHEN COALESCE(vs.eta_planner, tl_pred.predicted_eta_ml) IS NOT NULL AND COALESCE(vs.actual_ata, vs.ata) IS NOT NULL 
                THEN ABS(EXTRACT(EPOCH FROM (COALESCE(vs.actual_ata, vs.ata)::timestamp - COALESCE(vs.eta_planner, tl_pred.predicted_eta_ml)::timestamp)) / 3600.0)
                ELSE NULL
            END AS prediction_error_hours
        FROM vessel_schedules vs
        LEFT JOIN master_vessels mv ON vs.vessel_id = mv.id
        LEFT JOIN LATERAL (
            SELECT predicted_eta_ml
            FROM tracking_logs
            WHERE schedule_id = vs.id AND predicted_eta_ml IS NOT NULL
            ORDER BY scraped_at DESC
            LIMIT 1
        ) tl_pred ON true
        WHERE vs.service = p_service
          AND vs.status = 'Departed'
          AND (vs.actual_ata IS NOT NULL OR vs.ata IS NOT NULL)
    ),
    integrity_calc AS (
        SELECT
            COALESCE(
                AVG(
                    CASE WHEN liner_error_hours IS NOT NULL 
                    THEN GREATEST(0, 100.0 - (liner_error_hours / 24.0) * 100.0)
                    ELSE NULL END
                ),
                0
            ) AS liner_integrity_pct,
            COALESCE(
                AVG(
                    CASE WHEN prediction_error_hours IS NOT NULL 
                    THEN GREATEST(0, 100.0 - (prediction_error_hours / 24.0) * 100.0)
                    ELSE NULL END
                ),
                0
            ) AS prediction_integrity_pct,
            COUNT(CASE WHEN liner_error_hours IS NOT NULL THEN 1 END) AS liner_sample_count,
            COUNT(CASE WHEN prediction_error_hours IS NOT NULL THEN 1 END) AS prediction_sample_count,
            COUNT(*) AS total_departed
        FROM departed_with_ata
    )
    SELECT json_build_object(
        'liner_integrity_pct', ROUND(ic.liner_integrity_pct, 1),
        'prediction_integrity_pct', ROUND(ic.prediction_integrity_pct, 1),
        'liner_sample_count', ic.liner_sample_count,
        'prediction_sample_count', ic.prediction_sample_count,
        'total_departed', ic.total_departed,
        'records', COALESCE(
            (SELECT json_agg(json_build_object(
                'vessel_name', d.vessel_name,
                'voyage', d.voyage,
                'liner_eta', d.liner_eta,
                'predicted_eta', d.predicted_eta,
                'ata', d.ata,
                'liner_error_hours', ROUND(d.liner_error_hours::numeric, 1),
                'prediction_error_hours', ROUND(d.prediction_error_hours::numeric, 1)
            ) ORDER BY d.ata DESC)
            FROM departed_with_ata d),
            '[]'::json
        )
    ) INTO v_result
    FROM integrity_calc ic;

    RETURN v_result;
END;
$$ LANGUAGE plpgsql;

-- 5. Permissions
GRANT SELECT, INSERT ON public.liner_eta_history TO authenticated;
GRANT SELECT ON public.liner_eta_history TO anon;
GRANT EXECUTE ON FUNCTION public.get_service_eta_integrity(TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_service_eta_integrity(TEXT) TO anon;
