"""Published price-conflict representations and bounded, scoped SQL reads agree."""
import json
import uuid

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from drf_spectacular.generators import SchemaGenerator
from jsonschema import Draft4Validator,FormatChecker

from backups.tests.test_recovery_purchase_contract import nullable_schema
from catalogue.models import Product
from catalogue.tests import test_price_conflicts_api as fixtures


class PriceConflictContractTests(TestCase):
    setUp=fixtures.PriceConflictAPITests.setUp
    authenticate=fixtures.PriceConflictAPITests.authenticate
    post_conflict=fixtures.PriceConflictAPITests.post_conflict

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema=SchemaGenerator().get_schema(request=None,public=True)

    def assert_response_schema(self,response,ending,method='get'):
        path=next(path for path in self.schema['paths'] if path.endswith(ending) and '/packs/' not in path)
        rule=self.schema['paths'][path][method]['responses'][str(response.status_code)]['content']['application/json']['schema']
        validator=Draft4Validator(nullable_schema({**rule,'components':self.schema['components']}),format_checker=FormatChecker())
        errors=list(validator.iter_errors(json.loads(response.content)))
        self.assertEqual([(list(error.absolute_path),error.message) for error in errors],[])

    def close_at_common_price(self,conflict):
        detail=self.url+conflict['id']+'/'
        preview=self.client.get(detail+'preview/');self.assertEqual(preview.status_code,200,preview.content)
        command={'operation_id':str(uuid.uuid4()),'preview_token':preview.data['preview_token'],
            'proposal_id':None,'reason':'Maintien explicite du prix commun'}
        response=self.client.post(detail+'resolve/',command,format='json')
        self.assertEqual(response.status_code,200,response.content)
        return response,command

    def test_real_create_replay_list_detail_preview_resolution_and_public_bodies_match_openapi(self):
        response=self.post_conflict();self.assertEqual(response.status_code,201,response.content)
        conflict=response.data;detail=self.url+conflict['id']+'/'
        responses=[(response,'/price-conflicts/','post'),(self.post_conflict(),'/price-conflicts/','post'),
            (self.client.get(self.url),'/price-conflicts/','get'),(self.client.get(detail),'/price-conflicts/{id}/','get'),
            (self.client.get(detail+'preview/'),'/price-conflicts/{id}/preview/','get')]
        for body,ending,method in responses:
            with self.subTest(ending=ending,method=method,status=body.status_code):
                self.assertIn(body.status_code,(200,201),body.content);self.assert_response_schema(body,ending,method)
        self.authenticate(self.seller)
        public=self.client.get(self.product_url);self.assertEqual(public.status_code,200,public.content)
        self.assertNotIn('purchase_price_fcfa',public.data)
        self.assertEqual(public.data['price_conflict'],{'id':conflict['id'],'state':'open','common_price_fcfa':1000})
        self.assert_response_schema(public,'/products/{id}/')
        self.authenticate(self.owner)
        resolved,command=self.close_at_common_price(conflict)
        self.assert_response_schema(resolved,'/price-conflicts/{id}/resolve/','post')
        replay=self.client.post(detail+'resolve/',command,format='json')
        self.assertEqual(replay.data,resolved.data)
        self.assert_response_schema(replay,'/price-conflicts/{id}/resolve/','post')
        closed_preview=self.client.get(detail+'preview/')
        self.assertEqual((closed_preview.status_code,closed_preview.data['can_resolve'],closed_preview.data['preview_token']),(200,False,None))
        self.assert_response_schema(closed_preview,'/price-conflicts/{id}/preview/')
        self.authenticate(self.seller)
        final=self.client.get(self.product_url);self.assertEqual(final.status_code,200,final.content)
        self.assertIsNone(final.data['price_conflict']);self.assert_response_schema(final,'/products/{id}/')

    def test_seller_product_pages_do_not_add_a_price_query_for_every_row_or_load_costs(self):
        from catalogue.models import ProductPriceConflict
        conflict=self.post_conflict();self.assertEqual(conflict.status_code,201,conflict.content)
        Product.objects.bulk_create([Product(shop=self.shop,name=f'Produit public {index}',unit_kind='piece',unit_label='pièce',
            sale_price_fcfa=10,purchase_price_fcfa=987654) for index in range(30)])
        self.authenticate(self.seller)
        query_counts=[]
        for page_size in (1,25):
            with CaptureQueriesContext(connection) as queries:
                response=self.client.get(self.products+f'?page_size={page_size}')
            self.assertEqual(response.status_code,200,response.content)
            self.assertEqual((response.data['count'],len(response.data['results'])),(31,page_size))
            self.assertTrue(any(row['price_conflict'] for row in response.data['results']))
            for row in response.data['results']:self.assertNotIn('purchase_price_fcfa',row)
            price_queries=[row['sql'] for row in queries.captured_queries if ProductPriceConflict._meta.db_table in row['sql']]
            query_counts.append(len(price_queries))
            self.assertTrue(all('987654' not in row['sql'] for row in queries.captured_queries))
            # Private product columns are deferred on the actual page SELECT.
            pages=[row['sql'] for row in queries.captured_queries if 'FROM "core_product"' in row['sql'] and ' LIMIT ' in row['sql']]
            self.assertTrue(pages,queries.captured_queries)
            self.assertTrue(all('"core_product"."purchase_price_fcfa"' not in query for query in pages))
        self.assertEqual(query_counts[0],query_counts[1],query_counts)
        self.assertLessEqual(query_counts[1],2,query_counts)

    def test_manager_conflict_history_is_scoped_and_paginated_in_sql_before_serializing_proposals(self):
        from catalogue.models import ProductPriceConflict
        first=self.post_conflict();self.assertEqual(first.status_code,201,first.content)
        self.close_at_common_price(first.data)
        for index in (1,2):
            current=self.client.get(self.product_url).data
            change={'operation_id':str(uuid.uuid4()),'base_version':current['version'],
                'sale_price_fcfa':1100+index,'reason':'Nouvelle branche historique de qualification'}
            self.assertEqual(self.client.patch(self.product_url,change,format='json').status_code,200)
            conflict=self.post_conflict(operation_id=str(uuid.uuid4()),source_change_id=change['operation_id'],
                base_version=current['version'],proposed_price_fcfa=1200+index)
            self.assertEqual(conflict.status_code,201,conflict.content)
            self.close_at_common_price(conflict.data)
        with CaptureQueriesContext(connection) as queries:
            page=self.client.get(self.url+'?page_size=1')
        self.assertEqual(page.status_code,200,page.content)
        self.assertEqual((page.data['count'],len(page.data['results'])),(3,1))
        self.assertIsNotNone(page.data['next']);self.assertEqual(page.data['results'][0]['id'],first.data['id'])
        table=ProductPriceConflict._meta.db_table
        selects=[row['sql'] for row in queries.captured_queries if 'FROM "'+table+'"' in row['sql'] and ' LIMIT 1' in row['sql']]
        self.assertTrue(selects,queries.captured_queries)
        for query in selects:
            self.assertIn('"'+table+'"."shop_id"',query)
            self.assertIn('"'+table+'"."product_id"',query)
        second=self.client.get(page.data['next']);self.assertEqual(second.status_code,200,second.content)
        self.assertNotEqual(second.data['results'][0]['id'],first.data['id'])
