<?php
/**
 * Plugin Name: Xuanran SEO · SEOPress REST Bridge
 * Description: Exposes SEOPress post meta (_seopress_*) to the WP core REST API so the
 *              Xuanran SEO Blog Writer pipeline (incl. hermes_adapter) can write SEO fields via
 *              POST /wp-json/wp/v2/posts/{id} with a `meta` object, and read them back with
 *              ?context=edit for verification.
 *
 *              Drop this file at:  wp-content/mu-plugins/xuanran-seopress-rest-bridge.php
 *              MU-plugins auto-load. No activation needed. SEOPress (free) must be active.
 *
 *              Health probe:  GET /wp-json/xuanran/v1/seopress-bridge
 *
 * Why a bridge: SEOPress stores its fields as underscore-prefixed ("protected") post meta.
 * WordPress hides protected meta from REST unless a key is registered with show_in_rest and
 * an auth_callback. This file does exactly that, nothing else — it does not change how
 * SEOPress renders <title>, description, canonical or robots.
 *
 * Version:     1.0.0
 * Author:      Xuanran SEO Blog Writer
 * License:     GPL-2.0+
 * Requires at least: 5.6
 */

if ( ! defined( 'ABSPATH' ) ) { exit; }

const XUANRAN_SPB_VERSION = '1.0.0';

/** Only users who can edit this specific post may write its SEO meta (App Password user >= Editor). */
function xuanran_spb_auth( $allowed, $meta_key, $post_id, $user_id ) {
    return user_can( $user_id, 'edit_post', $post_id );
}

function xuanran_spb_text_keys() {
    return [
        '_seopress_titles_title',           // meta title (SEOPress variables like %%post_title%% OK)
        '_seopress_titles_desc',            // meta description
        '_seopress_analysis_target_kw',     // focus keywords, comma-separated
        '_seopress_robots_breadcrumbs',     // custom breadcrumb title
        '_seopress_social_fb_title',
        '_seopress_social_fb_desc',
        '_seopress_social_twitter_title',
        '_seopress_social_twitter_desc',
        // robots flags: SEOPress stores "yes" to ENABLE the restriction (yes = noindex), "" = default
        '_seopress_robots_index',
        '_seopress_robots_follow',
        '_seopress_robots_imageindex',
        '_seopress_robots_archive',
        '_seopress_robots_snippet',
    ];
}

function xuanran_spb_url_keys() {
    return [
        '_seopress_robots_canonical',
        '_seopress_social_fb_img',
        '_seopress_social_twitter_img',
    ];
}

function xuanran_spb_int_keys() {
    return [
        '_seopress_robots_primary_cat',     // term ID
        '_seopress_social_fb_img_attachment_id',
        '_seopress_social_twitter_img_attachment_id',
    ];
}

add_action( 'init', function () {
    foreach ( [ 'post', 'page' ] as $type ) {
        foreach ( xuanran_spb_text_keys() as $key ) {
            register_post_meta( $type, $key, [
                'type'              => 'string',
                'single'            => true,
                'show_in_rest'      => true,
                'sanitize_callback' => 'sanitize_text_field',
                'auth_callback'     => 'xuanran_spb_auth',
            ] );
        }
        foreach ( xuanran_spb_url_keys() as $key ) {
            register_post_meta( $type, $key, [
                'type'              => 'string',
                'single'            => true,
                'show_in_rest'      => true,
                'sanitize_callback' => 'esc_url_raw',
                'auth_callback'     => 'xuanran_spb_auth',
            ] );
        }
        foreach ( xuanran_spb_int_keys() as $key ) {
            register_post_meta( $type, $key, [
                'type'              => 'string',   // SEOPress stores these as strings
                'single'            => true,
                'show_in_rest'      => true,
                'sanitize_callback' => function ( $v ) { return $v === '' ? '' : (string) absint( $v ); },
                'auth_callback'     => 'xuanran_spb_auth',
            ] );
        }
    }
}, 20 );

add_action( 'rest_api_init', function () {
    register_rest_route( 'xuanran/v1', '/seopress-bridge', [
        'methods'             => 'GET',
        'permission_callback' => '__return_true',
        'callback'            => function () {
            return [
                'bridge_active'    => true,
                'bridge_version'   => XUANRAN_SPB_VERSION,
                'seopress_active'  => defined( 'SEOPRESS_VERSION' ),
                'seopress_version' => defined( 'SEOPRESS_VERSION' ) ? SEOPRESS_VERSION : null,
                'registered_keys'  => array_merge( xuanran_spb_text_keys(), xuanran_spb_url_keys(), xuanran_spb_int_keys() ),
            ];
        },
    ] );
} );
