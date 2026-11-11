-- Run once in a NEW, dedicated Supabase project SQL Editor.
-- Variable dimensions: exact scan initially, suitable for this small corpus.
begin;
create schema if not exists extensions;
create extension if not exists vector with schema extensions;
create table public.knowledge_indexes (
  tenant_id text not null,
  index_id text not null,
  profile jsonb not null,
  expected_count integer not null check (expected_count > 0),
  state text not null default 'building' check (state in ('building','ready')),
  primary key (tenant_id,index_id)
);
create table public.knowledge_chunks (
  tenant_id text not null,
  index_id text not null,
  chunk_id text not null,
  payload jsonb not null,
  embedding extensions.vector not null,
  primary key (tenant_id,index_id,chunk_id),
  foreign key (tenant_id,index_id) references public.knowledge_indexes(tenant_id,index_id),
  check ((payload->>'ingestion_allowed' = 'true') is true),
  check ((payload->>'retrieval_eligible' = 'true') is true),
  check ((payload->>'chunk_id' = chunk_id) is true)
);
alter table public.knowledge_indexes enable row level security;
alter table public.knowledge_chunks enable row level security;
-- No browser read/write policies. service_role is only used by trusted server/CLI.
revoke all on public.knowledge_indexes,public.knowledge_chunks from anon,authenticated;
grant select,insert,update on public.knowledge_indexes,public.knowledge_chunks to service_role;

create function public.register_knowledge_index(p_tenant text,p_index text,p_profile jsonb,p_expected integer)
returns text language plpgsql security invoker set search_path = '' as $$
declare r public.knowledge_indexes;
begin
  if p_expected < 1 or (p_profile->>'dimensions')::integer < 1 then raise exception 'Invalid index profile'; end if;
  insert into public.knowledge_indexes(tenant_id,index_id,profile,expected_count)
  values(p_tenant,p_index,p_profile,p_expected) on conflict do nothing;
  select * into strict r from public.knowledge_indexes where tenant_id=p_tenant and index_id=p_index for update;
  if r.profile <> p_profile or r.expected_count <> p_expected then raise exception 'Index profile mismatch'; end if;
  return r.state;
end $$;

create function public.finalize_knowledge_index(p_tenant text,p_index text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare r public.knowledge_indexes; n bigint; invalid bigint;
begin
  select * into strict r from public.knowledge_indexes where tenant_id=p_tenant and index_id=p_index for update;
  select count(*), count(*) filter (where extensions.vector_dims(embedding) <> (r.profile->>'dimensions')::integer)
  into n,invalid from public.knowledge_chunks where tenant_id=p_tenant and index_id=p_index;
  if n <> r.expected_count or invalid > 0 then raise exception 'Incomplete index or vector dimensions mismatch'; end if;
  update public.knowledge_indexes set state='ready' where tenant_id=p_tenant and index_id=p_index;
  return jsonb_build_object('state','ready','count',n,'index_id',p_index);
end $$;

create function public.match_knowledge_chunks(p_tenant text,p_index text,p_vector extensions.vector,p_limit integer,p_threshold double precision)
returns table(chunk_id text,similarity double precision)
language plpgsql stable security invoker set search_path = '' as $$
declare r public.knowledge_indexes;
begin
  select * into strict r from public.knowledge_indexes where tenant_id=p_tenant and index_id=p_index;
  if r.state <> 'ready' then raise exception 'Index not ready'; end if;
  if extensions.vector_dims(p_vector) <> (r.profile->>'dimensions')::integer then raise exception 'Query vector dimensions mismatch'; end if;
  if p_limit < 1 or p_limit > 100 or p_threshold < -1 or p_threshold > 1 then raise exception 'Invalid search limits'; end if;
  return query select c.chunk_id,1-(c.embedding operator(extensions.<=>) p_vector)
  from public.knowledge_chunks c
  where c.tenant_id=p_tenant and c.index_id=p_index
    and 1-(c.embedding operator(extensions.<=>) p_vector) >= p_threshold
  order by c.embedding operator(extensions.<=>) p_vector,c.chunk_id limit p_limit;
end $$;
revoke all on function public.register_knowledge_index(text,text,jsonb,integer) from public,anon,authenticated;
revoke all on function public.finalize_knowledge_index(text,text) from public,anon,authenticated;
revoke all on function public.match_knowledge_chunks(text,text,extensions.vector,integer,double precision) from public,anon,authenticated;
grant execute on function public.register_knowledge_index(text,text,jsonb,integer) to service_role;
grant execute on function public.finalize_knowledge_index(text,text) to service_role;
grant execute on function public.match_knowledge_chunks(text,text,extensions.vector,integer,double precision) to service_role;
commit;
