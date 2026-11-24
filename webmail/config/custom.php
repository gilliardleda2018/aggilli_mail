<?php
$config['language'] = 'pt_BR';
$config['product_name'] = 'Aggilli Mail';
$config['login_rate_limit'] = 5;            // tentativas de login por minuto
$config['managesieve_host'] = 'tls://mail.aggillintelligence.com.br';
$config['managesieve_port'] = 4190;
$config['x_frame_options'] = false;          // permite abrir dentro do painel (só via Tailscale)
$config['session_lifetime'] = 60;
$config['skin_logo'] = ['elastic:*' => 'skins/elastic/images/aggilli-logo.png'];
