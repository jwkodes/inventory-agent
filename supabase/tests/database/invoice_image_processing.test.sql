begin;

create extension if not exists pgtap with schema extensions;

select plan(11);

select has_function(
  'public',
  'claim_telegram_image_event',
  array['uuid'],
  'atomic Telegram image claim function exists'
);
select has_function(
  'public',
  'claim_next_telegram_image_event',
  array[]::text[],
  'continuous image worker claim function exists'
);

insert into public.source_events (
  id,
  organization_id,
  provider,
  external_event_id,
  event_type,
  payload
)
values (
  '50000000-0000-0000-0000-000000000011',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'invoice-image-photo',
  'invoice_image',
  '{
    "message": {
      "from": {"id": 100000001},
      "chat": {"id": -100123456789},
      "caption": "Supplier delivery",
      "photo": [
        {
          "file_id": "small-photo",
          "file_unique_id": "photo-unique",
          "width": 90,
          "height": 120,
          "file_size": 1000
        },
        {
          "file_id": "large-photo",
          "file_unique_id": "photo-unique",
          "width": 900,
          "height": 1200,
          "file_size": 10000
        }
      ]
    }
  }'::jsonb
);

create temporary table claimed_photo as
select * from public.claim_telegram_image_event(
  '50000000-0000-0000-0000-000000000011'
);

select is((select count(*) from claimed_photo), 1::bigint, 'photo event is claimed');
select is(
  (select telegram_file_id from claimed_photo),
  'large-photo',
  'claim chooses the largest Telegram photo size'
);
select is(
  (select media_type from claimed_photo),
  'image/jpeg',
  'Telegram photos are recorded as JPEG'
);
select is(
  (select caption from claimed_photo),
  'Supplier delivery',
  'claim retains the optional caption'
);
select is(
  (select width from claimed_photo),
  900,
  'claim retains selected image dimensions'
);
select ok(
  public.finish_source_event(
    '50000000-0000-0000-0000-000000000011',
    true,
    null
  ),
  'claimed photo can be completed'
);

insert into public.source_events (
  id,
  organization_id,
  provider,
  external_event_id,
  event_type,
  payload
)
values (
  '50000000-0000-0000-0000-000000000012',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'invoice-image-document',
  'invoice_image',
  '{
    "message": {
      "from": {"id": 100000001},
      "chat": {"id": 100000001},
      "document": {
        "file_id": "png-document",
        "file_unique_id": "png-unique",
        "file_name": "supplier invoice.png",
        "mime_type": "image/png",
        "file_size": 12345
      }
    }
  }'::jsonb
);

create temporary table claimed_document as
select * from public.claim_next_telegram_image_event();

select is(
  (select event_id from claimed_document),
  '50000000-0000-0000-0000-000000000012'::uuid,
  'claim-next returns the eligible image document'
);
select is(
  (select original_file_name from claimed_document),
  'supplier invoice.png',
  'claim preserves the original document name'
);
select is(
  (select media_type from claimed_document),
  'image/png',
  'claim preserves the image document media type'
);

select * from finish();
rollback;
