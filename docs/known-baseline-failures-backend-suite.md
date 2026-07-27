---
title: Known pre-existing backend-suite failures (baseline, unrelated to any one ticket)
category: convention
module: knowledge/serve
component: test-suite
tags: [backend-suite, box-service, ci]
applies_when: the universal backend-suite check (knowledge/serve/tests) is run for a ticket in af-build-remote-jobs
---

# Known pre-existing `knowledge/serve/tests` failures

Verified on `af-build/praxis` at `88747b3` (R67 ticket start) by stashing every remote-jobs
change and re-running the full suite: identical failures with zero box-service/remote-jobs code
present. This is drift accumulated from other already-merged tickets against the shared
Postgres instance (`localhost:5437`), not a regression any single ticket introduced. This list
supersedes the smaller one previously verified at `e1a920e` — the drift widened as more tickets
landed against the same shared database.

Deterministic (fail every run, confirmed by a stash/no-stash A-B comparison, 82 failed / 572
passed either way):

- `test_batch_parallel.py::test_parallel_batch_distinct_items_all_land`
- `test_batch_parallel.py::test_parallel_batch_preserves_same_batch_dedup`
- `test_compounding_read_surface.py::test_context_as_of_excludes_later_fact`
- `test_compounding_read_surface.py::test_meta_round_trips_through_candidate_detail`
- `test_compounding_read_surface.py::test_stale_derivations_after_source_rejected`
- `test_episodic_memory.py::test_context_excludes_episodic_by_default`
- `test_episodic_memory.py::test_context_excludes_episodic_from_mounted_overlay`
- `test_episodic_memory.py::test_contradicting_episode_does_not_supersede_earlier`
- `test_episodic_memory.py::test_episode_canonical_fields_win_over_caller_keys`
- `test_episodic_memory.py::test_episode_preserves_caller_defined_meta_keys`
- `test_episodic_memory.py::test_record_episode_via_http_stores_episodic`
- `test_episodic_memory.py::test_two_episodes_same_topic_survive_unmerged`
- `test_mounts.py::test_mount_non_member_404`
- `test_mounts.py::test_mount_unknown_snapshot_404`
- `test_mounts.py::test_mounted_snapshot_is_recalled_but_not_merged_or_saved`
- `test_mounts.py::test_mounted_store_crud`
- `test_org_sharing.py::test_browse_member_snapshot_facts_grouped`
- `test_org_sharing.py::test_browse_non_member_org_header_is_403`
- `test_org_sharing.py::test_browse_snapshot_reads_cached_not_live`
- `test_org_sharing.py::test_fold_in_carries_edge_between_two_selected_facts`
- `test_org_sharing.py::test_fold_in_contradiction_reports_conflict_without_overwrite`
- `test_org_sharing.py::test_fold_in_copies_facts_with_provenance_active`
- `test_org_sharing.py::test_fold_in_dedups_identical_fact_caller_already_holds`
- `test_org_sharing.py::test_fold_in_from_snapshot_reads_cached_not_live`
- `test_org_sharing.py::test_fold_in_replace_mode_truncates_caller_graph_first`
- `test_org_sharing.py::test_fold_in_unknown_source_user_is_404`
- `test_org_sharing.py::test_org_sources_lists_members_and_snapshots`
- `test_server.py::test_batch_ingest_happy_path`
- `test_server.py::test_batch_ingest_persists_writer_metadata`
- `test_server.py::test_clear_graph_empties_the_users_graph`
- `test_server.py::test_context_hits_include_provenance_keys`
- `test_server.py::test_create_list_get_candidate`
- `test_server.py::test_delete_active_succeeds_without_reject`
- `test_server.py::test_delete_proposed_and_rejected_succeed`
- `test_server.py::test_delete_then_get_is_404`
- `test_server.py::test_edit_auto_resolve_supersedes_clashing_fact`
- `test_server.py::test_edit_default_is_literal_write`
- `test_server.py::test_edit_rejects_unknown_on_conflict`
- `test_server.py::test_edit_surface_mode_raises_resolvable_contradiction`
- `test_server.py::test_graph_reflects_active_facts`
- `test_server.py::test_graph_state_all_includes_proposed`
- `test_server.py::test_ingest_derived_from_creates_edge`
- `test_server.py::test_insight_derived_fills_unset_metadata`
- `test_server.py::test_insight_derived_from_creates_edge`
- `test_server.py::test_insight_persists_writer_metadata`
- `test_server.py::test_insight_then_context_round_trips`
- `test_server.py::test_insights_batch_bad_item_does_not_abort_batch`
- `test_server.py::test_insights_batch_writes_all_and_confirms_retrievable`
- `test_server.py::test_insights_surface_mode_keeps_both_and_surfaces_contradiction`
- `test_server.py::test_patch_clearing_content_still_400s`
- `test_server.py::test_patch_meta_only_succeeds_without_meta_title`
- `test_server.py::test_patch_updates_candidate`
- `test_server.py::test_promote_advances_proposed_to_active`
- `test_server.py::test_record_failure_demotes_fact_utility`
- `test_server.py::test_record_outcome_accepts_boolean`
- `test_server.py::test_record_outcome_requires_boolean_success`
- `test_server.py::test_reject_rejects`
- `test_server.py::test_rename_snapshot_onto_existing_name_is_409`
- `test_server.py::test_rename_snapshot_rekeys_and_preserves_count`
- `test_server.py::test_resolve_keep_all_keeps_both_active_and_clears_pending`
- `test_server.py::test_resolve_keep_none_rejects_all_and_clears_pending`
- `test_server.py::test_resolve_keep_subset_of_three`
- `test_server.py::test_resolve_rejects_bad_keep_id`
- `test_server.py::test_snapshots_save_list_load_delete_round_trip`
- `test_session_ingest.py::test_happy_path_creates_proposed_candidates`
- `test_session_ingest.py::test_omitted_source_is_autogenerated`
- `test_session_ingest.py::test_secret_in_narrative_not_stored_verbatim`
- `test_space_org_delete.py::test_delete_org_owner_purges_facts_keys_and_membership`
- `test_space_org_delete.py::test_delete_space_leaves_default_graph_intact`
- `test_space_org_delete.py::test_delete_space_unlists_and_purges_graph`
- `test_space_org_delete.py::test_raw_batch_keeps_all_near_duplicates`
- `test_space_org_delete.py::test_rename_space_changes_name_not_id_or_graph`
- `test_spaces.py::test_create_rejects_reserved_and_malformed_slugs`
- `test_spaces.py::test_empty_space_header_falls_back_to_default`
- `test_spaces.py::test_no_space_header_uses_principal_sub`
- `test_spaces.py::test_space_fact_stored_under_namespaced_user_id`
- `test_spaces.py::test_space_facts_isolated_from_default`
- `test_spaces.py::test_two_spaces_are_mutually_isolated`
- `test_spaces.py::test_unknown_space_is_404_on_read`
- `test_spaces.py::test_unknown_space_is_404_on_write`
- `test_surface_bindings.py::test_bind_read_and_reject_requirement_for_surface`
- `test_surface_bindings.py::test_coverage_flags_uncovered_surface_and_requirement`

Most surface as `400 Bad Request: "space required"`, a `default` org/space already existing from
earlier accumulated test/session state, or a stale response shape — pre-existing API/test/DB
drift, not test-order or Postgres-contention flakiness (confirmed reproducible with an identical
82-failed/572-passed split whether or not this ticket's changes are present).

This list is excluded (`--deselect`) from this ticket's own backend-suite validation run so the
ticket is graded on whether **it** regressed the suite, not on unrelated pre-existing breakage —
mirroring `agent-factory-suite`'s own documented `-k "not test_factory_project_from_dotenv..."`
exclusion for a different tracked pre-existing bug. Remove entries here once each underlying bug
is actually fixed by whichever ticket owns that surface; do not leave this permanently. This
ticket's own new/touched files (`knowledge/serve/box_service_failures.py`,
`knowledge/serve/box_service_models.py`, `knowledge/serve/tests/test_failure_paths.py`) carry
none of this drift — `test_failure_paths.py` is 100% green, standalone and in the full suite.
