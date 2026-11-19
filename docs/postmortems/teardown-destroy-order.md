# Postmortem: the destroy of prod stopped three times on its own order

- Duration: one teardown session, from the first `terraform destroy` of prod to a passing `make teardown`
- Kind: real incident of the migration's own operations; nothing published, no data exposed
- Severity: low (prod was already out of service); the window's cost was not at risk, but a
  teardown left half done keeps billing storage and leaves identities in the tenant
- Status: closed; each cause fixed in the configuration and the procedure

## Summary

The plan of [ADR 0007](../adr/0007-azure-infrastructure-identities-and-cost.md) held in its order of
roots (bundle, prod, bootstrap) but not inside the prod root. Terraform orders a destroy by the
reverse of the references between resources, and three kinds of objects have dependencies that no
reference expresses. Each stopped the destroy part way; each was finished by hand on the live
platform, then removed from the state. Afterwards `make teardown` found one more object that
Terraform reported destroyed but had only deactivated.

Final result: 50 objects of 2 states looked up after the destroy, none left; 0 resource groups and
0 resources in the subscription; the account's metastore serves no workspace
([teardown](../assessment/teardown.md)).

## Sequence

| Step | Event |
|---|---|
| 1 | CI destroys the bundle as its deployer: 17 objects (schemas with their tables and volumes, jobs) |
| 2 | `make infra-destroy-plan ROOT=prod`, then `make infra-destroy`: the workspace bindings go first, having no dependent. The grants, the catalog and the external locations then fail with `not accessible in current workspace` |
| 3 | The four bindings are restored through the Unity Catalog API; the destroy is planned again and goes on |
| 4 | The external location `amlmig-prod-catalog` is refused: 49 managed tables and 1 managed volume dropped in step 1 are still soft-deleted under it. Deleted with `force=true`, then released from the state |
| 5 | The two role assignments are refused with 403: the operator destroying is Contributor, which cannot delete role assignments. Released from the state; Azure deletes them with the storage accounts they are scoped to |
| 6 | Prod's state is empty; the bootstrap is moved to a local state and destroyed |
| 7 | `make teardown` fails: 1 object left, the deployer service principal of the Databricks account, present and `active: false` |
| 8 | The service principal is deleted through the account SCIM API; `make teardown` passes |

## Root causes

1. **Workspace bindings are a precondition of their securable, not a dependent.** An isolated
   catalog, credential or location is reachable from the bound workspace only. The binding
   references the securable, so Terraform destroys the binding first, and every later call on that
   securable from the workspace fails.
2. **A managed table outlives its `DROP`.** Unity Catalog keeps a dropped managed table for its
   retention period under the managed location, and refuses to delete an external location that
   still holds data unless forced. The bundle's destroy dropped the tables minutes before.
3. **Destroying needed a right the destroying operator did not have.** The role assignments were
   created by an identity allowed to assign roles; the operator destroying held Contributor only.
4. **The account-level service principal is deactivated, not deleted, by default.** The provider's
   `disable_as_user_deletion` defaults to true at account level; Terraform reports a destroy, the
   identity stays.

The first three were found by the destroy itself. The fourth would have gone unnoticed without the
lookup of every identifier after it, which is the point of `make teardown`.

## What changed

- `make infra-destroy-plan` releases the workspace bindings and role assignments from the state
  after keeping their identifiers and before planning: they go with what they attach to, and the
  teardown still looks them up.
- The external locations carry `force_destroy`: they are only ever destroyed with the whole window,
  when the soft-deleted tables under them have no reader left.
- The deployer service principal sets `disable_as_user_deletion = false`.
- `lakemig teardown inventory` merges a new plan into the kept inventory by address instead of
  replacing the root: planned again after a partial destroy, the state no longer holds what is
  already gone, and those objects must still be looked up.

## What went well

The inventory taken before the first destroy is what made the result checkable after three
interruptions and a change of state backend: every object was looked up by the identifier it had,
not by what the last state still listed.
