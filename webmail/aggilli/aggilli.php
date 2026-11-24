<?php
/** Identidade Aggilli no Roundcube: logo maior e centralizada no login, cores da marca. */
class aggilli extends rcube_plugin
{
    public function init()
    {
        $this->include_stylesheet('aggilli.css');
    }
}
