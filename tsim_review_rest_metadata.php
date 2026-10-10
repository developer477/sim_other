<?php
/**
 * Install on both WooCommerce sites, or append to the existing review snippet.
 * Expose only the recorded variation fields through the WooCommerce review API.
 */
add_filter('woocommerce_rest_prepare_product_review', function ($response, $review, $request) {
    $fields = $request->get_param('_fields');
    if ($fields) {
        $include_meta = false;
        foreach (wp_parse_list($fields) as $field) {
            if ($field === 'meta_data' || strpos($field, 'meta_data.') === 0) {
                $include_meta = true;
                break;
            }
        }
        if (!$include_meta) {
            return $response;
        }
    }

    $data = $response->get_data();
    $meta = isset($data['meta_data']) ? $data['meta_data'] : array();
    foreach (array('_review_variation_id', '_review_variation_sku', '_review_variation_name') as $key) {
        $value = get_comment_meta($review->comment_ID, $key, true);
        if ($value !== '') {
            $meta[] = array('key' => $key, 'value' => $value);
        }
    }
    $data['meta_data'] = $meta;
    $response->set_data($data);
    return $response;
}, 10, 3);
