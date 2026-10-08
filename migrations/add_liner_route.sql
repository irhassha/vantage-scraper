-- =============================================================================
-- Migration: Add liner_route to vessels_status_view (Safe In-Place Replacement)
--
-- Uses CREATE OR REPLACE VIEW with vs.liner_route appended at the end.
-- NO DROP VIEW ... CASCADE is used, so all dependent views, materialized views,
-- and triggers are completely preserved.
-- =============================================================================

CREATE OR REPLACE VIEW public.vessels_status_view AS
SELECT 
    vs.id,
    mv.vessel_name,
    mv.id AS master_vessel_id,
    mv.imo_number,
    vs.voyage AS voyage_number,
    vs.service,
    vs.liner_eta,
    vs.etb,
    vs.is_watchlist,
    vs.status,
    vs.previous_port,
    vs.actual_atb,
    vs.actual_ata,
    vs.skipped_ports,
    vs.custom_route,
    COALESCE(tl.speed_sog, vs.speed_sog) AS speed_sog,
    tl.preview_eta_ais,
    COALESCE(tl.distance_to_jkt, vs.distance_to_jkt_nm) AS distance_to_jkt,
    tl.destination,
    tl.latitude,
    tl.longitude,
    tl.scraped_at AS last_updated,
    metric.avg_speed_24h,
    metric.avg_speed_12h,
    metric.historical_transit_hours,
    metric.fallback_assumed_ports,
    metric.fallback_port_stay_hours,
    metric.eta_calculation_reason,
    -- Appended at the very end to allow PostgreSQL in-place view replacement
    vs.liner_route
FROM vessel_schedules vs
LEFT JOIN master_vessels mv ON vs.vessel_id = mv.id
LEFT JOIN LATERAL (
    SELECT speed_sog, preview_eta_ais, distance_to_jkt, destination, latitude, longitude, scraped_at
    FROM tracking_logs
    WHERE schedule_id = vs.id
    ORDER BY scraped_at DESC
    LIMIT 1
) tl ON true
LEFT JOIN LATERAL get_vessel_eta_metrics(vs.id, tl.destination, COALESCE(tl.distance_to_jkt, vs.distance_to_jkt_nm)) metric ON true;

GRANT SELECT ON public.vessels_status_view TO authenticated;

-- If vessels_status_snapshot exists, safely refresh it
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_matviews WHERE schemaname = 'public' AND matviewname = 'vessels_status_snapshot'
  ) THEN
    PERFORM public.refresh_vessels_status_snapshot();
  END IF;
END $$;
