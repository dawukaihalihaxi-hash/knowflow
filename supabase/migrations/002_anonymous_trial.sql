-- Additive migration in the EXISTING project. No changes to existing vectors.
begin;
create table if not exists public.knowflow_trial_workspace (
 id text primary key, manifest jsonb not null default '{}',
 revision bigint not null default 0, lease text, lease_until timestamptz
);
create table if not exists public.knowflow_trial_usage (
 workspace text not null, subject text not null, period text not null,
 kind text not null, used integer not null check(used >= 0),
 primary key(workspace,subject,period,kind)
);
alter table public.knowflow_trial_workspace enable row level security;
alter table public.knowflow_trial_usage enable row level security;
revoke all on public.knowflow_trial_workspace,public.knowflow_trial_usage from public,anon,authenticated;
grant select,insert,update on public.knowflow_trial_workspace,public.knowflow_trial_usage to service_role;
create or replace function public.knowflow_trial_lock(p_workspace text,p_token text,p_seconds integer)
returns boolean language plpgsql security invoker set search_path='' as $$
begin
 insert into public.knowflow_trial_workspace(id) values(p_workspace) on conflict do nothing;
 update public.knowflow_trial_workspace set lease=p_token,lease_until=clock_timestamp()+make_interval(secs=>least(greatest(p_seconds,10),600))
 where id=p_workspace and (lease=p_token or lease_until is null or lease_until<clock_timestamp());
 return found;
end $$;
create or replace function public.knowflow_trial_commit(p_workspace text,p_token text,p_manifest jsonb)
returns bigint language plpgsql security invoker set search_path='' as $$
declare n bigint;
begin
 update public.knowflow_trial_workspace set manifest=p_manifest,revision=revision+1,lease_until=clock_timestamp()+interval '300 seconds'
 where id=p_workspace and lease=p_token and lease_until>clock_timestamp() returning revision into n;
 if n is null then raise exception 'Workspace lease expired'; end if;
 return n;
end $$;
create or replace function public.knowflow_trial_unlock(p_workspace text,p_token text)
returns void language sql security invoker set search_path='' as $$
 update public.knowflow_trial_workspace set lease=null,lease_until=null where id=p_workspace and lease=p_token;
$$;
create or replace function public.knowflow_trial_consume(p_workspace text,p_checks jsonb)
returns boolean language plpgsql security invoker set search_path='' as $$
declare c jsonb; n integer;
begin
 perform pg_advisory_xact_lock(hashtextextended(p_workspace,0));
 if jsonb_typeof(p_checks)<>'array' then raise exception 'Invalid quota request'; end if;
 for c in select value from jsonb_array_elements(p_checks) loop
  if (c->>'amount')::integer<0 or (c->>'limit')::integer<1 then raise exception 'Invalid quota'; end if;
  select used into n from public.knowflow_trial_usage where workspace=p_workspace and subject=c->>'subject' and period=c->>'period' and kind=c->>'kind';
  if coalesce(n,0)+(c->>'amount')::integer>(c->>'limit')::integer then return false; end if;
 end loop;
 for c in select value from jsonb_array_elements(p_checks) loop
  insert into public.knowflow_trial_usage(workspace,subject,period,kind,used)
  values(p_workspace,c->>'subject',c->>'period',c->>'kind',(c->>'amount')::integer)
  on conflict(workspace,subject,period,kind) do update set used=public.knowflow_trial_usage.used+excluded.used;
 end loop;
 return true;
end $$;
revoke all on function public.knowflow_trial_lock(text,text,integer),public.knowflow_trial_commit(text,text,jsonb),public.knowflow_trial_unlock(text,text),public.knowflow_trial_consume(text,jsonb) from public,anon,authenticated;
grant execute on function public.knowflow_trial_lock(text,text,integer),public.knowflow_trial_commit(text,text,jsonb),public.knowflow_trial_unlock(text,text),public.knowflow_trial_consume(text,jsonb) to service_role;
commit;
